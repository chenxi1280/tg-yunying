from __future__ import annotations

import hashlib

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import (
    Action,
    FulfillmentFactProjectionState,
    FulfillmentObligationProjection,
    FulfillmentRemoteFact,
    RemoteReconcileCase,
    SearchClickAssignment,
    SearchClickFulfillmentObligation,
)
from app.services._common import _now

from .fact_first_insert import insert_do_nothing


DECISION_FACT_KIND = "unknown_deadline_closed"
PROJECTION_KINDS = ("obligation", "action", "task_read_model")


def close_unknown_after_deadline(session: Session, *, limit: int) -> int:
    rows = list(session.execute(
        select(Action.id, Action.action_version)
        .where(
            Action.status == "unknown_after_send",
            Action.unknown_deadline_at.is_not(None),
            Action.unknown_deadline_at <= _now(),
        )
        .order_by(Action.unknown_deadline_at, Action.id)
        .limit(max(1, limit))
    ))
    return sum(_close_unknown_action(session, action_id, version) for action_id, version in rows)


def _close_unknown_action(session: Session, action_id: str, version: int) -> int:
    action = session.get(Action, action_id)
    if action is None or not _close_action(session, action_id, version):
        return 0
    fact = _append_deadline_fact(session, action)
    _close_obligation_projection(session, action)
    _close_reconcile_case(session, action_id)
    _close_business_unknown(session, action)
    _complete_fact_projections(session, fact.fact_id)
    return 1


def _close_action(session: Session, action_id: str, version: int) -> bool:
    changed = session.execute(
        update(Action)
        .where(
            Action.id == action_id,
            Action.status == "unknown_after_send",
            Action.action_version == version,
        )
        .values(
            status="closed_unknown",
            action_version=version + 1,
            lease_owner="",
            lease_expires_at=None,
            claim_owner="",
            claim_token="",
            claim_expires_at=None,
        )
    ).rowcount
    return changed == 1


def _append_deadline_fact(session: Session, action: Action) -> FulfillmentRemoteFact:
    source = session.scalar(
        select(FulfillmentRemoteFact)
        .where(
            FulfillmentRemoteFact.action_id == action.id,
            FulfillmentRemoteFact.fact_kind == "remote_outcome_unknown",
        )
        .order_by(FulfillmentRemoteFact.observed_at.desc())
        .limit(1)
    )
    if source is None:
        raise RuntimeError("unknown_deadline_source_fact_missing")
    values = _deadline_fact_values(action, source)
    insert_do_nothing(
        session,
        FulfillmentRemoteFact,
        values,
        columns=("remote_mutation_key_hash", "gateway_request_hash", "fact_kind"),
    )
    fact = session.scalar(select(FulfillmentRemoteFact).where(
        FulfillmentRemoteFact.remote_mutation_key_hash == source.remote_mutation_key_hash,
        FulfillmentRemoteFact.gateway_request_hash == source.gateway_request_hash,
        FulfillmentRemoteFact.fact_kind == DECISION_FACT_KIND,
    ))
    if fact is None:
        raise RuntimeError("unknown_deadline_fact_insert_missing")
    _ensure_fact_projections(session, fact.fact_id)
    return fact


def _deadline_fact_values(action: Action, source: FulfillmentRemoteFact) -> dict:
    identity = hashlib.sha256(
        f"{source.remote_mutation_key_hash}:{source.gateway_request_hash}:{DECISION_FACT_KIND}".encode()
    ).hexdigest()
    outcome = {
        **dict(source.outcome or {}),
        "decision": DECISION_FACT_KIND,
        "unknown_deadline_at": (
            action.unknown_deadline_at.isoformat()
            if action.unknown_deadline_at
            else None
        ),
    }
    return {
        "tenant_id": source.tenant_id,
        "task_type": source.task_type,
        "task_id": source.task_id,
        "task_day_ledger_id": source.task_day_ledger_id,
        "obligation_type": source.obligation_type,
        "obligation_id": source.obligation_id,
        "action_id": source.action_id,
        "attempt_id": source.attempt_id,
        "mutation_kind": source.mutation_kind,
        "remote_mutation_key_hash": source.remote_mutation_key_hash,
        "gateway_request_hash": source.gateway_request_hash,
        "fact_kind": DECISION_FACT_KIND,
        "fact_identity_hash": identity,
        "outcome": outcome,
        "observed_at": _now(),
    }


def _ensure_fact_projections(session: Session, fact_id: str) -> None:
    for kind in PROJECTION_KINDS:
        insert_do_nothing(
            session,
            FulfillmentFactProjectionState,
            {
                "fact_id": fact_id,
                "projection_kind": kind,
                "expected_target_version": 0,
                "state": "pending",
                "next_retry_at": _now(),
            },
            columns=("fact_id", "projection_kind"),
        )


def _complete_fact_projections(session: Session, fact_id: str) -> None:
    session.execute(
        update(FulfillmentFactProjectionState)
        .where(
            FulfillmentFactProjectionState.fact_id == fact_id,
            FulfillmentFactProjectionState.state.in_(("pending", "failed")),
        )
        .values(state="projected", projected_at=_now(), updated_at=_now())
    )


def _close_obligation_projection(session: Session, action: Action) -> None:
    session.execute(
        update(FulfillmentObligationProjection)
        .where(
            FulfillmentObligationProjection.obligation_type == action.obligation_type,
            FulfillmentObligationProjection.obligation_id == action.obligation_id,
            FulfillmentObligationProjection.state == "remote_reconcile_only",
        )
        .values(
            state="closed_with_unknown_shortfall",
            version=FulfillmentObligationProjection.version + 1,
        )
    )


def _close_reconcile_case(session: Session, action_id: str) -> None:
    session.execute(
        update(RemoteReconcileCase)
        .where(
            RemoteReconcileCase.action_id == action_id,
            RemoteReconcileCase.state == "open",
        )
        .values(state="closed_unknown", next_probe_at=None, updated_at=_now())
    )


def _close_business_unknown(session: Session, action: Action) -> None:
    if action.task_type != "search_click":
        return
    payload = dict(action.payload or {})
    assignment_id = str(payload.get("search_click_assignment_id") or "")
    obligation_id = str(payload.get("search_click_obligation_id") or "")
    assignment = session.get(SearchClickAssignment, assignment_id) if assignment_id else None
    obligation = session.get(SearchClickFulfillmentObligation, obligation_id) if obligation_id else None
    if assignment is not None and assignment.state == "gateway_unknown":
        assignment.state = "closed_unknown"
        assignment.version = int(assignment.version or 1) + 1
    if (
        obligation is not None
        and obligation.source_action_id == action.id
        and obligation.status != "confirmed"
    ):
        obligation.status = "closed_unknown"


__all__ = ["close_unknown_after_deadline"]

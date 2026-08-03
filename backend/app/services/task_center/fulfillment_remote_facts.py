from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session, object_session

from app.models import (
    Action,
    ExecutionAttempt,
    FulfillmentFactProjectionState,
    FulfillmentObligationProjection,
    FulfillmentRemoteFact,
    GatewayRequestEvidenceJournal,
)
from app.services._common import _now
from .fact_first_insert import insert_do_nothing as _insert_do_nothing


PROJECTION_KINDS = ("obligation", "action", "task_read_model")
UNKNOWN_RECONCILE_DEADLINE_SECONDS = 1800


def ensure_action_obligation(session: Session, action: Action) -> bool:
    obligation_type, obligation_id = _obligation_identity(action)
    duplicate = _duplicate_open_action(
        session,
        action,
        obligation_type=obligation_type,
        obligation_id=obligation_id,
    )
    if duplicate is not None:
        _skip_obligation_action(
            action,
            "duplicate_open_obligation",
            existing_action_id=duplicate,
        )
        return False
    _bind_action_identity(action, obligation_type, obligation_id)
    _insert_obligation_projection(session, action)
    projection = _obligation_projection(session, obligation_type, obligation_id)
    if projection.state != "open":
        _skip_obligation_action(
            action,
            "obligation_not_open",
            obligation_state=projection.state,
        )
        return False
    _rebind_projection(session, action, projection)
    session.flush()
    return True


def _duplicate_open_action(
    session: Session,
    action: Action,
    *,
    obligation_type: str,
    obligation_id: str,
) -> str | None:
    return session.scalar(
        select(Action.id)
        .where(
            Action.obligation_type == obligation_type,
            Action.obligation_id == obligation_id,
            Action.id != action.id,
            Action.status.in_((
                "pending",
                "claiming",
                "executing",
                "unknown_after_send",
            )),
        )
        .limit(1)
    )


def _bind_action_identity(
    action: Action,
    obligation_type: str,
    obligation_id: str,
) -> None:
    action.obligation_type = obligation_type
    action.obligation_id = obligation_id
    action.execution_lane = _execution_lane(action)


def _insert_obligation_projection(session: Session, action: Action) -> None:
    values = {
        "tenant_id": action.tenant_id,
        "task_id": action.task_id,
        "task_day_ledger_id": _payload(action).get("task_day_ledger_id"),
        "task_lifecycle_epoch": int(action.task_lifecycle_epoch or 1),
        "obligation_type": action.obligation_type,
        "obligation_id": action.obligation_id,
        "work_lane": action.execution_lane,
        "opened_at": action.created_at or _now(),
        "deadline_at": _deadline(action),
        "materialization_version": int(action.materialization_version or 1),
        "state": "open",
        "active_action_id": action.id,
        "version": 1,
    }
    _insert_do_nothing(
        session,
        FulfillmentObligationProjection,
        values,
        columns=("obligation_type", "obligation_id"),
    )


def _obligation_projection(
    session: Session,
    obligation_type: str,
    obligation_id: str,
) -> FulfillmentObligationProjection:
    projection = session.scalar(select(FulfillmentObligationProjection).where(
        FulfillmentObligationProjection.obligation_type == obligation_type,
        FulfillmentObligationProjection.obligation_id == obligation_id,
    ))
    if projection is None:
        raise RuntimeError("fulfillment_obligation_projection_missing")
    return projection


def _rebind_projection(
    session: Session,
    action: Action,
    projection: FulfillmentObligationProjection,
) -> None:
    if projection.active_action_id == action.id:
        return
    version = int(projection.version or 1)
    materialization_version = int(projection.materialization_version or 1) + 1
    changed = session.execute(
        update(FulfillmentObligationProjection)
        .where(
            FulfillmentObligationProjection.id == projection.id,
            FulfillmentObligationProjection.state == "open",
            FulfillmentObligationProjection.version == version,
        )
        .values(
            active_action_id=action.id,
            materialization_version=materialization_version,
            version=version + 1,
        )
    ).rowcount
    if changed != 1:
        raise ValueError("fulfillment_obligation_materialization_conflict")
    action.materialization_version = materialization_version
    session.expire(projection)


def _skip_obligation_action(action: Action, code: str, **detail) -> None:
    action.status = "skipped"
    action.executed_at = _now()
    action.result = {
        **dict(action.result or {}),
        "success": False,
        "error_code": code,
        **detail,
    }


def persist_remote_fact(session: Session, action: Action) -> FulfillmentRemoteFact | None:
    attempt = _latest_attempt(session, action.id)
    if attempt is None or not _fact_worthy(action, attempt):
        return None
    values = _fact_values(action, attempt)
    _insert_do_nothing(
        session,
        FulfillmentRemoteFact,
        values,
        columns=("remote_mutation_key_hash", "gateway_request_hash", "fact_kind"),
    )
    fact = session.scalar(select(FulfillmentRemoteFact).where(
        FulfillmentRemoteFact.remote_mutation_key_hash == values["remote_mutation_key_hash"],
        FulfillmentRemoteFact.gateway_request_hash == values["gateway_request_hash"],
        FulfillmentRemoteFact.fact_kind == values["fact_kind"],
    ))
    if fact is None:
        raise RuntimeError("remote_fact_insert_missing")
    _ensure_projection_states(session, fact)
    _set_unknown_deadline(action, fact.fact_kind)
    session.flush()
    return fact


def project_remote_fact(session: Session, fact: FulfillmentRemoteFact) -> None:
    projection = session.scalar(select(FulfillmentObligationProjection).where(
        FulfillmentObligationProjection.obligation_type == fact.obligation_type,
        FulfillmentObligationProjection.obligation_id == fact.obligation_id,
    ))
    if projection is None:
        raise RuntimeError("remote_fact_obligation_projection_missing")
    next_state = _projection_state(fact.fact_kind)
    expected_version = int(projection.version or 1)
    changed = session.execute(
        update(FulfillmentObligationProjection)
        .where(
            FulfillmentObligationProjection.id == projection.id,
            FulfillmentObligationProjection.version == expected_version,
        )
        .values(state=next_state, version=expected_version + 1)
    ).rowcount
    if changed not in {0, 1}:
        raise RuntimeError("remote_fact_projection_cas_invalid")
    if changed == 0:
        session.expire(projection)
        if projection.state != next_state:
            raise RuntimeError("remote_fact_projection_cas_conflict")
    _complete_projection_state(session, fact.fact_id, "obligation")


def complete_derived_projections(session: Session, fact_id: str) -> None:
    _complete_projection_state(session, fact_id, "action")
    _complete_projection_state(session, fact_id, "task_read_model")


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
    if action is None:
        return 0
    changed = session.execute(
        update(Action)
        .where(
            Action.id == action_id,
            Action.status == "unknown_after_send",
            Action.action_version == version,
        )
        .values(
            status="closed_with_unknown_shortfall",
            action_version=version + 1,
            lease_owner="",
            lease_expires_at=None,
            claim_owner="",
            claim_token="",
            claim_expires_at=None,
        )
    ).rowcount
    if changed != 1:
        return 0
    session.execute(
        update(FulfillmentObligationProjection)
        .where(
            FulfillmentObligationProjection.obligation_type == action.obligation_type,
            FulfillmentObligationProjection.obligation_id == action.obligation_id,
            FulfillmentObligationProjection.state == "remote_reconcile_only",
        )
        .values(state="closed_with_unknown_shortfall", version=FulfillmentObligationProjection.version + 1)
    )
    from app.models import RemoteReconcileCase

    session.execute(
        update(RemoteReconcileCase)
        .where(
            RemoteReconcileCase.action_id == action_id,
            RemoteReconcileCase.state == "open",
        )
        .values(state="closed_unknown", next_probe_at=None, updated_at=_now())
    )
    _close_business_unknown(session, action)
    return 1


def _close_business_unknown(session: Session, action: Action) -> None:
    if action.task_type != "search_click":
        return
    from app.models import SearchClickAssignment, SearchClickFulfillmentObligation

    payload = _payload(action)
    assignment_id = str(payload.get("search_click_assignment_id") or "")
    obligation_id = str(payload.get("search_click_obligation_id") or "")
    assignment = session.get(SearchClickAssignment, assignment_id) if assignment_id else None
    obligation = session.get(SearchClickFulfillmentObligation, obligation_id) if obligation_id else None
    if assignment is not None and assignment.state == "gateway_unknown":
        assignment.state = "closed_unknown"
        assignment.version = int(assignment.version or 1) + 1
    if obligation is not None and obligation.status == "unknown_after_send":
        obligation.status = "closed_unknown"


def _fact_values(action: Action, attempt: ExecutionAttempt) -> dict:
    fact_kind = _fact_kind(action, attempt)
    request_hash = _hash(_request_identity(action, attempt))
    mutation_hash = _hash(_mutation_identity(action))
    identity = _hash(f"{mutation_hash}:{request_hash}:{fact_kind}")
    return {
        "tenant_id": action.tenant_id,
        "task_type": action.task_type,
        "task_id": action.task_id,
        "task_day_ledger_id": _payload(action).get("task_day_ledger_id"),
        "obligation_type": str(action.obligation_type or _obligation_identity(action)[0]),
        "obligation_id": str(action.obligation_id or _obligation_identity(action)[1]),
        "action_id": action.id,
        "attempt_id": attempt.id,
        "mutation_kind": action.action_type,
        "remote_mutation_key_hash": mutation_hash,
        "gateway_request_hash": request_hash,
        "fact_kind": fact_kind,
        "fact_identity_hash": identity,
        "outcome": _fact_outcome(action, attempt),
        "observed_at": attempt.after_call_at or _now(),
    }


def _fact_kind(action: Action, attempt: ExecutionAttempt) -> str:
    if action.status == "unknown_after_send":
        return "remote_outcome_unknown"
    if action.status != "success" or attempt.status != "success":
        return (
            "safely_not_executed"
            if _remote_mutation_state(action, attempt) == "false"
            else "remote_outcome_unknown"
        )
    result = dict(action.result or {})
    if action.action_type in {"search_join", "search_join_membership"}:
        return "target_click_observed" if result.get("target_click_observed") else "remote_outcome_unknown"
    if action.action_type == "view_message":
        return "view_observed"
    if action.action_type == "like_message":
        return "reaction_observed"
    return "remote_message_observed" if attempt.remote_message_id else "remote_outcome_unknown"


def _fact_outcome(action: Action, attempt: ExecutionAttempt) -> dict:
    result = dict(action.result or {})
    return {
        "action_status": action.status,
        "attempt_status": attempt.status,
        "remote_message_id": attempt.remote_message_id,
        "remote_fact_id": result.get("remote_fact_id"),
        "target_click_observed": bool(result.get("target_click_observed")),
        "failure_type": attempt.failure_type,
    }


def _fact_worthy(action: Action, attempt: ExecutionAttempt) -> bool:
    return bool(
        attempt.gateway_call_started_at
        or action.status in {"success", "unknown_after_send"}
    )


def _ensure_projection_states(session: Session, fact: FulfillmentRemoteFact) -> None:
    for kind in PROJECTION_KINDS:
        _insert_do_nothing(
            session,
            FulfillmentFactProjectionState,
            {
                "fact_id": fact.fact_id,
                "projection_kind": kind,
                "expected_target_version": 0,
                "state": "pending",
                "next_retry_at": _now(),
            },
            columns=("fact_id", "projection_kind"),
        )


def _complete_projection_state(session: Session, fact_id: str, kind: str) -> None:
    session.execute(
        update(FulfillmentFactProjectionState)
        .where(
            FulfillmentFactProjectionState.fact_id == fact_id,
            FulfillmentFactProjectionState.projection_kind == kind,
            FulfillmentFactProjectionState.state.in_(("pending", "failed")),
        )
        .values(state="projected", projected_at=_now(), updated_at=_now())
    )


def _set_unknown_deadline(action: Action, fact_kind: str) -> None:
    if fact_kind != "remote_outcome_unknown" or action.unknown_deadline_at is not None:
        return
    action.status = "unknown_after_send"
    action.unknown_deadline_at = _now() + timedelta(
        seconds=UNKNOWN_RECONCILE_DEADLINE_SECONDS
    )


def _projection_state(fact_kind: str) -> str:
    if fact_kind == "safely_not_executed":
        return "open"
    if fact_kind == "remote_outcome_unknown":
        return "remote_reconcile_only"
    return "confirmed"


def _obligation_identity(action: Action) -> tuple[str, str]:
    if action.obligation_type and action.obligation_id:
        return str(action.obligation_type), str(action.obligation_id)
    payload = _payload(action)
    keys = (
        ("search_click", "search_click_fulfillment_obligation_id"),
        ("comment", "comment_fulfillment_obligation_id"),
        ("view", "view_fulfillment_obligation_id"),
        ("reaction", "reaction_fulfillment_obligation_id"),
        ("coverage", "coverage_ledger_id"),
        ("quantity_slot", "primary_quantity_slot_id"),
    )
    for kind, key in keys:
        if payload.get(key):
            return kind, str(payload[key])
    return action.action_type, str(action.action_dedupe_key or action.id)


def _execution_lane(action: Action) -> str:
    if action.action_type in {"search_join", "search_join_membership"}:
        return "search"
    if action.action_type in {"group_bot_control_observation"}:
        return "observation"
    return "interaction"


def _deadline(action: Action):
    payload = _payload(action)
    value = payload.get("obligation_deadline_at") or payload.get("deadline_at")
    if isinstance(value, datetime) or value is None:
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("fulfillment_obligation_deadline_invalid") from exc


def _remote_mutation_state(action: Action, attempt: ExecutionAttempt) -> str:
    session = object_session(action)
    if session is not None:
        journal = session.scalar(
            select(GatewayRequestEvidenceJournal)
            .where(
                GatewayRequestEvidenceJournal.action_id == action.id,
                GatewayRequestEvidenceJournal.execution_attempt_id == attempt.id,
            )
            .limit(1)
        )
        if journal is not None:
            return str(journal.remote_mutation_state or "unknown")
    observed = dict(attempt.result_snapshot or {}).get("remote_mutation_started")
    return "true" if observed is True else "false" if observed is False else "unknown"


def _request_identity(action: Action, attempt: ExecutionAttempt) -> str:
    payload = _payload(action)
    return str(
        payload.get("gateway_request_identity")
        or payload.get("request_identity")
        or f"{action.id}:{attempt.attempt_no}"
    )


def _mutation_identity(action: Action) -> str:
    payload = _payload(action)
    return str(
        payload.get("remote_mutation_key")
        or action.action_dedupe_key
        or f"{action.task_id}:{action.id}"
    )


def _latest_attempt(session: Session, action_id: str) -> ExecutionAttempt | None:
    return session.scalar(
        select(ExecutionAttempt)
        .where(ExecutionAttempt.action_id == action_id)
        .order_by(ExecutionAttempt.attempt_no.desc())
        .limit(1)
    )


def _payload(action: Action) -> dict:
    return dict(action.payload or {})


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()
__all__ = [
    "close_unknown_after_deadline",
    "complete_derived_projections",
    "ensure_action_obligation",
    "persist_remote_fact",
    "project_remote_fact",
]

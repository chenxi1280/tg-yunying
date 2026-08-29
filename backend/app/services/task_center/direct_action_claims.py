from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import case, func, or_, select, update
from sqlalchemy.orm import Session

from app.models import (
    AccountPacingReservation,
    Action,
    ExecutionAttempt,
    FulfillmentRemoteFact,
    SourcePacingAdmission,
    SourcePacingState,
    Task,
)

from .fulfillment_activation import CURRENT_CONTRACT_VERSION
from .account_pacing_guard import revalidate_action_pacing_before_claim
from .channel_action_lifecycle import (
    release_channel_action_resources_before_gateway,
    validate_channel_action_resources_released,
)
from .channel_remote_evidence import action_remote_mutation_evidence
from .fulfillment_remote_facts import (
    ensure_action_obligation,
    persist_remote_fact,
    project_remote_fact,
)
from .fulfillment_ledger_owners import align_view_ledger_for_safe_settlement
from .source_pacing import wall_datetime


SAFE_SETTLEMENT_ACTION_STATUSES = frozenset({
    "cancelled",
    "claiming",
    "executing",
    "failed",
    "pending",
    "retryable_failed",
    "skipped",
})


@dataclass(frozen=True)
class DirectClaimBatch:
    action_ids: tuple[str, ...]
    owner: str
    token: str


def claim_fact_first_candidates(
    session: Session,
    *,
    owner: str,
    limit: int,
    now: datetime,
    lease_seconds: int,
    exclude_task_ids: set[str] | None = None,
    execution_lane: str | None = None,
    shard_total: int = 1,
    shard_index: int = 0,
) -> DirectClaimBatch:
    token = str(uuid4())
    candidates = _candidate_rows(
        session,
        limit=limit,
        now=now,
        exclude_task_ids=exclude_task_ids,
        execution_lane=execution_lane,
        shard_total=shard_total,
        shard_index=shard_index,
    )
    claimed = _claim_rows(
        session,
        candidates,
        owner=owner,
        token=token,
        now=now,
        lease_seconds=lease_seconds,
    )
    session.commit()
    return DirectClaimBatch(tuple(claimed), owner, token)


def _candidate_rows(
    session: Session,
    *,
    limit: int,
    now: datetime,
    exclude_task_ids: set[str] | None,
    execution_lane: str | None,
    shard_total: int = 1,
    shard_index: int = 0,
) -> list[tuple[str, str, int]]:
    ranked = (
        select(
            Action.id.label("action_id"),
            Action.task_id.label("task_id"),
            Action.action_version.label("action_version"),
            Action.scheduled_at.label("scheduled_at"),
            func.row_number().over(
                partition_by=Action.task_id,
                order_by=_candidate_order(),
            ).label("task_rank"),
        )
        .join(Task, Task.id == Action.task_id)
        .where(
            Action.status == "pending",
            or_(Action.scheduled_at <= now, _deadline_exhausted_action()),
            _has_claimable_account_reservation(),
            Task.status == "running",
            Task.deleted_at.is_(None),
            Task.fulfillment_contract_version == CURRENT_CONTRACT_VERSION,
            Action.task_lifecycle_epoch == Task.task_lifecycle_epoch,
            or_(
                Action.task_type != "group_ai_chat",
                Action.action_type != "send_message",
                func.coalesce(Action.payload["message_text"].as_string(), "") != "",
                func.coalesce(Action.payload["ai_generation_status"].as_string(), "") == "",
            ),
        )
    )
    if exclude_task_ids:
        ranked = ranked.where(Action.task_id.not_in(exclude_task_ids))
    ranked = _filter_execution_lane(ranked, execution_lane)
    ranked = _filter_account_shard(ranked, shard_total, shard_index)
    rows = ranked.subquery()
    statement = (
        select(rows.c.action_id, rows.c.task_id, rows.c.action_version)
        .order_by(rows.c.task_rank, rows.c.scheduled_at, rows.c.task_id, rows.c.action_id)
        .limit(max(1, limit))
    )
    return list(session.execute(statement))


def _deadline_exhausted_action():
    return select(AccountPacingReservation.id).where(
        AccountPacingReservation.action_id == Action.id,
        AccountPacingReservation.state.in_(("reserved", "bound")),
        AccountPacingReservation.source_deadline_at.is_not(None),
        Action.release_not_before_at.is_not(None),
        AccountPacingReservation.source_deadline_at <= Action.release_not_before_at,
    ).exists()


def _has_claimable_account_reservation():
    open_reservation = select(AccountPacingReservation.id).where(
        AccountPacingReservation.tenant_id == Action.tenant_id,
        AccountPacingReservation.account_id == Action.account_id,
        AccountPacingReservation.pacing_slot_key == Action.pacing_slot_key,
        AccountPacingReservation.state.in_(("reserved", "bound")),
    ).exists()
    return or_(
        Action.pacing_slot_key.is_(None),
        Action.pacing_slot_key == "",
        Action.account_id.is_(None),
        open_reservation,
    )


def _candidate_order():
    return (
        case((_deadline_exhausted_action(), 0), else_=1),
        Action.scheduled_at,
        Action.id,
    )


def _filter_execution_lane(statement, execution_lane: str | None):
    if execution_lane == "search":
        return statement.where(Action.execution_lane == "search")
    elif execution_lane == "non_search":
        return statement.where(or_(
            Action.execution_lane.is_(None),
            Action.execution_lane != "search",
        ))
    return statement


def _filter_account_shard(statement, shard_total: int, shard_index: int):
    total = max(1, int(shard_total or 1))
    index = max(0, min(total - 1, int(shard_index or 0)))
    if total == 1:
        return statement
    return statement.where(or_(
        Action.account_id.is_(None),
        (Action.account_id % total) == index,
    ))


def _claim_rows(
    session: Session,
    rows: list[tuple[str, str, int]],
    *,
    owner: str,
    token: str,
    now: datetime,
    lease_seconds: int,
) -> list[str]:
    claimed: list[str] = []
    cancelled_state_ids: set[str] = set()
    expires_at = now + timedelta(seconds=max(5, lease_seconds))
    for action_id, _, version in rows:
        action = _lock_candidate_action(session, action_id, version)
        if action is None:
            continue
        pacing = revalidate_action_pacing_before_claim(
            session, action, now_value=now,
        )
        if not pacing.allowed:
            if pacing.reason_code == "pacing_claim_deadline_exceeded":
                cancelled_state_ids.update(_mark_claim_deadline_missed(
                    session, action, now, pacing.effective_claim_at,
                ))
            session.flush()
            continue
        changed = session.execute(
            update(Action)
            .where(
                Action.id == action_id,
                Action.status == "pending",
                Action.action_version == version,
            )
            .values(
                status="claiming",
                claim_owner=owner,
                claim_token=token,
                claim_expires_at=expires_at,
                action_version=version + 1,
            )
        ).rowcount
        if changed == 1:
            claimed.append(action_id)
    reconcile_source_pacing_states(session, cancelled_state_ids)
    return claimed


def _lock_candidate_action(
    session: Session,
    action_id: str,
    version: int,
) -> Action | None:
    return session.scalar(
        select(Action)
        .where(
            Action.id == action_id,
            Action.status == "pending",
            Action.action_version == version,
        )
        .with_for_update(skip_locked=True)
    )


def _mark_claim_deadline_missed(
    session: Session,
    action: Action,
    now: datetime,
    effective_at: datetime | None,
) -> set[str]:
    return settle_fact_first_action_before_gateway(
        session,
        action,
        now=now,
        reason_code="pacing_claim_deadline_exceeded",
        detail="账号时间线冲突已越过来源截止时间，未调用 Gateway",
        effective_at=effective_at,
    )


def settle_fact_first_action_before_gateway(
    session: Session,
    action: Action,
    *,
    now: datetime,
    reason_code: str,
    detail: str,
    effective_at: datetime | None = None,
    replan_same_obligation: bool = False,
) -> set[str]:
    existing_fact = _safe_settlement_fact(session, action.id)
    if existing_fact is not None:
        _validate_safe_settlement_replay(session, action)
        return set()
    if action.status not in SAFE_SETTLEMENT_ACTION_STATUSES:
        raise RuntimeError(f"pre_gateway_safe_settlement_status_invalid:{action.status}")
    remote_mutation_state = _prepare_safe_settlement_action(session, action)
    action.status = "skipped"
    action.executed_at = action.executed_at or now
    action.action_version = int(action.action_version or 1) + 1
    action.result = _safe_settlement_result(
        action,
        reason_code=reason_code,
        detail=detail,
        effective_at=effective_at,
    )
    attempt = _safe_shortfall_attempt(
        session,
        action,
        now,
        reason_code=reason_code,
        detail=detail,
    )
    session.add(attempt)
    session.flush()
    fact = persist_remote_fact(session, action)
    if fact is None or fact.fact_kind != "safely_not_executed":
        observed = fact.fact_kind if fact is not None else "missing"
        raise RuntimeError(f"pre_gateway_safe_settlement_fact_missing:{observed}")
    project_remote_fact(session, fact)
    return release_fact_first_action_reservations(
        session,
        action,
        fact_kind=fact.fact_kind,
        remote_mutation_state=remote_mutation_state,
        replan_same_obligation=replan_same_obligation,
    )


def _prepare_safe_settlement_action(session: Session, action: Action) -> str | None:
    ledger_alignment = align_view_ledger_for_safe_settlement(session, action)
    if ledger_alignment:
        action.result = {
            **dict(action.result or {}),
            "pre_gateway_view_ledger_alignment": ledger_alignment,
        }
    evidence = action_remote_mutation_evidence(session, action)
    if evidence.state not in {None, "false"}:
        raise RuntimeError(
            f"pre_gateway_safe_settlement_remote_evidence_unsafe:{evidence.state}"
        )
    remote_mutation_state = evidence.state
    if not ensure_action_obligation(session, action):
        raise RuntimeError("pre_gateway_safe_settlement_obligation_unavailable")
    return remote_mutation_state


def _safe_settlement_fact(
    session: Session,
    action_id: str,
) -> FulfillmentRemoteFact | None:
    return session.scalar(
        select(FulfillmentRemoteFact)
        .where(
            FulfillmentRemoteFact.action_id == action_id,
            FulfillmentRemoteFact.fact_kind == "safely_not_executed",
        )
        .order_by(FulfillmentRemoteFact.observed_at.desc())
        .limit(1)
    )


def _validate_safe_settlement_replay(session: Session, action: Action) -> None:
    if action.status != "skipped":
        raise RuntimeError("safe_settlement_replay_action_not_skipped")
    validate_channel_action_resources_released(session, action)


def release_fact_first_action_reservations(
    session: Session,
    action: Action,
    *,
    fact_kind: str,
    remote_mutation_state: str | None = None,
    replan_same_obligation: bool = False,
) -> set[str]:
    if fact_kind != "safely_not_executed":
        return set()
    return release_channel_action_resources_before_gateway(
        session,
        action,
        remote_mutation_state=remote_mutation_state,
        replan_same_obligation=replan_same_obligation,
    )


def _safe_settlement_result(
    action: Action,
    *,
    reason_code: str,
    detail: str,
    effective_at: datetime | None,
) -> dict:
    result = {
        **(action.result or {}),
        "success": False,
        "error_code": reason_code,
        "error_message": detail,
        "remote_mutation_started": False,
        "pre_gateway_safe_settlement": {"reason_code": reason_code},
    }
    if reason_code == "pacing_claim_deadline_exceeded":
        result[reason_code] = {
            "effective_claim_at": effective_at.isoformat() if effective_at else None,
        }
    return result


def reconcile_source_pacing_states(
    session: Session,
    state_ids: set[str],
) -> None:
    if not state_ids:
        return
    states = list(session.scalars(
        select(SourcePacingState)
        .where(SourcePacingState.id.in_(state_ids))
        .order_by(SourcePacingState.id)
        .with_for_update()
    ))
    session.flush()
    reserved = _reserved_source_tails(session, state_ids)
    for state in states:
        candidates = [wall_datetime(reserved[state.id])] if state.id in reserved else []
        if state.last_call_started_at is not None:
            candidates.append(
                wall_datetime(state.last_call_started_at)
                + timedelta(seconds=max(0, int(state.last_source_gap_seconds or 0)))
            )
        state.next_call_not_before_at = max(candidates) if candidates else None
        state.version = int(state.version or 1) + 1


def _reserved_source_tails(
    session: Session,
    state_ids: set[str],
) -> dict[str, datetime]:
    tails: dict[str, datetime] = {}
    rows = session.execute(select(
        SourcePacingAdmission.source_pacing_state_id,
        SourcePacingAdmission.call_not_before_at,
        SourcePacingAdmission.source_gap_seconds,
    ).where(
        SourcePacingAdmission.source_pacing_state_id.in_(state_ids),
        SourcePacingAdmission.state == "reserved",
    ))
    for state_id, not_before, gap_seconds in rows:
        tail = wall_datetime(not_before) + timedelta(
            seconds=max(0, int(gap_seconds or 0)),
        )
        tails[state_id] = max(tails.get(state_id, tail), tail)
    return tails


def _safe_shortfall_attempt(
    session: Session,
    action: Action,
    now: datetime,
    *,
    reason_code: str,
    detail: str,
) -> ExecutionAttempt:
    attempt_no = session.scalar(select(func.max(ExecutionAttempt.attempt_no)).where(
        ExecutionAttempt.action_id == action.id,
    )) or 0
    return ExecutionAttempt(
        tenant_id=action.tenant_id,
        action_id=action.id,
        task_lifecycle_epoch=int(action.task_lifecycle_epoch or 1),
        account_id=action.account_id,
        attempt_no=int(attempt_no) + 1,
        status="failed",
        before_call_at=now,
        after_call_at=now,
        failure_type=reason_code,
        failure_detail=detail,
        result_snapshot={"remote_mutation_started": False},
    )


__all__ = [
    "DirectClaimBatch",
    "claim_fact_first_candidates",
    "reconcile_source_pacing_states",
    "release_fact_first_action_reservations",
    "settle_fact_first_action_before_gateway",
]

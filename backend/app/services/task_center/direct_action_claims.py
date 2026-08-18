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
    SourcePacingAdmission,
    SourcePacingState,
    Task,
)

from .fulfillment_activation import CURRENT_CONTRACT_VERSION
from .account_pacing_guard import revalidate_action_pacing_before_claim
from .channel_fulfillment import release_channel_action_before_gateway
from .fulfillment_remote_facts import (
    ensure_action_obligation,
    persist_remote_fact,
    project_remote_fact,
)
from .source_pacing import wall_datetime


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
) -> DirectClaimBatch:
    token = str(uuid4())
    candidates = _candidate_rows(
        session,
        limit=limit,
        now=now,
        exclude_task_ids=exclude_task_ids,
        execution_lane=execution_lane,
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
            Task.status == "running",
            Task.deleted_at.is_(None),
            Task.fulfillment_contract_version == CURRENT_CONTRACT_VERSION,
            Action.task_lifecycle_epoch == Task.task_lifecycle_epoch,
            or_(
                Action.task_type != "group_ai_chat",
                Action.action_type != "send_message",
                func.coalesce(Action.payload["message_text"].as_string(), "") != "",
                func.coalesce(
                    Action.payload["ai_generation_status"].as_string(), ""
                ) == "",
            ),
        )
    )
    if exclude_task_ids:
        ranked = ranked.where(Action.task_id.not_in(exclude_task_ids))
    ranked = _filter_execution_lane(ranked, execution_lane)
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
) -> set[str]:
    if action.status not in {"pending", "skipped"}:
        raise RuntimeError(f"pre_gateway_safe_settlement_status_invalid:{action.status}")
    if not ensure_action_obligation(session, action):
        raise RuntimeError("pre_gateway_safe_settlement_obligation_unavailable")
    action.status = "skipped"
    action.executed_at = action.executed_at or now
    action.action_version = int(action.action_version or 1) + 1
    action.result = _safe_settlement_result(
        action,
        reason_code=reason_code,
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
    )


def release_fact_first_action_reservations(
    session: Session,
    action: Action,
    *,
    fact_kind: str,
) -> set[str]:
    if fact_kind != "safely_not_executed":
        return set()
    if action.action_type not in {"view_message", "like_message"}:
        return set()
    if not action.pacing_slot_key:
        return set()
    result = dict(action.result or {})
    terminal = (
        action.status == "skipped"
        or result.get("account_task_disposition") == "abandoned"
    )
    if not terminal:
        return set()
    _mark_pacing_reservation_missed(session, action.id)
    state_ids = _cancel_pre_gateway_source_admissions(session, action.id)
    release_channel_action_before_gateway(session, action)
    return state_ids


def _safe_settlement_result(
    action: Action,
    *,
    reason_code: str,
    effective_at: datetime | None,
) -> dict:
    result = {
        **(action.result or {}),
        "error_code": reason_code,
        "pre_gateway_safe_settlement": {"reason_code": reason_code},
    }
    if reason_code == "pacing_claim_deadline_exceeded":
        result[reason_code] = {
            "effective_claim_at": effective_at.isoformat() if effective_at else None,
        }
    return result


def _cancel_pre_gateway_source_admissions(
    session: Session,
    action_id: str,
) -> set[str]:
    state_ids = set(session.scalars(
        select(SourcePacingAdmission.source_pacing_state_id)
        .where(
            SourcePacingAdmission.action_id == action_id,
            SourcePacingAdmission.state.in_(("reserved", "finished")),
        )
        .distinct()
    ))
    if not state_ids:
        return set()
    list(session.scalars(
        select(SourcePacingState)
        .where(SourcePacingState.id.in_(state_ids))
        .order_by(SourcePacingState.id)
        .with_for_update()
    ))
    rows = session.scalars(
        select(SourcePacingAdmission)
        .outerjoin(ExecutionAttempt, ExecutionAttempt.id == SourcePacingAdmission.attempt_id)
        .where(
            SourcePacingAdmission.action_id == action_id,
            SourcePacingAdmission.state.in_(("reserved", "finished")),
            or_(
                SourcePacingAdmission.attempt_id.is_(None),
                ExecutionAttempt.gateway_call_started_at.is_(None),
            ),
        )
        .with_for_update(of=SourcePacingAdmission)
    )
    cancelled: set[str] = set()
    for admission in rows:
        admission.state = "cancelled_pre_gateway"
        admission.version = int(admission.version or 1) + 1
        cancelled.add(admission.source_pacing_state_id)
    return cancelled


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


def _mark_pacing_reservation_missed(session: Session, action_id: str) -> None:
    reservation = session.scalar(select(AccountPacingReservation).where(
        AccountPacingReservation.action_id == action_id,
    ))
    if reservation is None:
        raise RuntimeError("pacing_claim_reservation_missing")
    if reservation.state == "missed":
        return
    if reservation.state not in {"reserved", "bound"}:
        raise RuntimeError(
            f"pacing_claim_reservation_state_invalid:{reservation.state}"
        )
    reservation.state = "missed"
    reservation.version = int(reservation.version or 1) + 1


__all__ = [
    "DirectClaimBatch",
    "claim_fact_first_candidates",
    "reconcile_source_pacing_states",
    "release_fact_first_action_reservations",
    "settle_fact_first_action_before_gateway",
]

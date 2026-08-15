from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from app.models import AccountPacingReservation, Action, ExecutionAttempt, Task

from .fulfillment_activation import CURRENT_CONTRACT_VERSION
from .account_pacing_guard import revalidate_action_pacing_before_claim
from .fulfillment_remote_facts import (
    ensure_action_obligation,
    persist_remote_fact,
    project_remote_fact,
)


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
                order_by=(Action.scheduled_at, Action.id),
            ).label("task_rank"),
        )
        .join(Task, Task.id == Action.task_id)
        .where(
            Action.status == "pending",
            Action.scheduled_at <= now,
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
    expires_at = now + timedelta(seconds=max(5, lease_seconds))
    for action_id, _, version in rows:
        action = session.get(Action, action_id)
        if action is None or int(action.action_version or 1) != int(version):
            continue
        pacing = revalidate_action_pacing_before_claim(
            session, action, now_value=now,
        )
        if not pacing.allowed:
            if pacing.reason_code == "pacing_claim_deadline_exceeded":
                _mark_claim_deadline_missed(
                    session, action, now, pacing.effective_claim_at,
                )
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
    return claimed


def _mark_claim_deadline_missed(
    session: Session,
    action: Action,
    now: datetime,
    effective_at: datetime | None,
) -> None:
    ensure_action_obligation(session, action)
    action.status = "skipped"
    action.executed_at = now
    action.action_version = int(action.action_version or 1) + 1
    action.result = {
        **(action.result or {}),
        "error_code": "pacing_claim_deadline_exceeded",
        "pacing_claim_deadline_exceeded": {
            "effective_claim_at": effective_at.isoformat() if effective_at else None,
        },
    }
    session.add(_safe_shortfall_attempt(session, action, now))
    _mark_pacing_reservation_missed(session, action.id)
    session.flush()
    fact = persist_remote_fact(session, action)
    if fact is None or fact.fact_kind != "safely_not_executed":
        observed = fact.fact_kind if fact is not None else "missing"
        raise RuntimeError(f"pacing_claim_safe_settlement_missing:{observed}")
    project_remote_fact(session, fact)


def _safe_shortfall_attempt(
    session: Session,
    action: Action,
    now: datetime,
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
        failure_type="pacing_claim_deadline_exceeded",
        failure_detail="账号时间线冲突已越过来源截止时间，未调用 Gateway",
        result_snapshot={"remote_mutation_started": False},
    )


def _mark_pacing_reservation_missed(session: Session, action_id: str) -> None:
    reservation = session.scalar(select(AccountPacingReservation).where(
        AccountPacingReservation.action_id == action_id,
        AccountPacingReservation.state.in_(("reserved", "bound")),
    ))
    if reservation is None:
        raise RuntimeError("pacing_claim_reservation_missing")
    reservation.state = "missed"
    reservation.version = int(reservation.version or 1) + 1


__all__ = ["DirectClaimBatch", "claim_fact_first_candidates"]

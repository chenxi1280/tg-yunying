from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from app.models import Action, Task

from .fulfillment_activation import CURRENT_CONTRACT_VERSION


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


__all__ = ["DirectClaimBatch", "claim_fact_first_candidates"]

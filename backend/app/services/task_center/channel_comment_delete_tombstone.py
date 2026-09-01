from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Action,
    ChannelCommentPlanContract,
    ChannelCommentPlanLifecycleEvent,
    CommentFulfillmentObligation,
    ExecutionAttempt,
    Task,
    TaskDeleteOperationItem,
)


COMMENT_OUTCOME_ITEM = "channel_comment_outcome"
COMMENT_LIFECYCLE_MUTATION = "channel_comment_lifecycle"


@dataclass(frozen=True)
class CommentOutcomeSnapshot:
    plan_id: str
    state_hash: str
    terminal_state: str
    remote_started: bool
    reconcile_state: str


def comment_outcome_snapshots(
    session: Session,
    task_id: str,
) -> list[CommentOutcomeSnapshot]:
    plans = list(session.scalars(select(ChannelCommentPlanContract).where(
        ChannelCommentPlanContract.task_id == task_id,
    ).order_by(ChannelCommentPlanContract.id)))
    if not plans:
        return []
    plan_ids = [plan.id for plan in plans]
    events = _events_by_plan(session, plan_ids)
    obligations = _obligations_by_plan(session, plan_ids)
    remote_action_ids = _remote_action_ids(
        session,
        [row.current_action_id for rows in obligations.values() for row in rows],
    )
    return [
        _snapshot(plan, events.get(plan.id, []), obligations.get(plan.id, []), remote_action_ids)
        for plan in plans
    ]


def comment_tombstone_values(
    session: Session,
    task: Task,
    operation_id: str,
) -> list[dict]:
    items = list(session.scalars(select(TaskDeleteOperationItem).where(
        TaskDeleteOperationItem.operation_id == operation_id,
        TaskDeleteOperationItem.entity_type == COMMENT_OUTCOME_ITEM,
    ).order_by(TaskDeleteOperationItem.entity_id)))
    snapshots = {
        row.plan_id: row for row in comment_outcome_snapshots(session, task.id)
    }
    values = []
    for item in items:
        snapshot = snapshots.get(item.entity_id)
        if snapshot is None or snapshot.state_hash != item.expected_state_hash:
            raise ValueError("physical_delete_comment_outcome_changed")
        values.append(_tombstone_value(task, snapshot))
    return values


def _snapshot(
    plan: ChannelCommentPlanContract,
    events: list[ChannelCommentPlanLifecycleEvent],
    obligations: list[CommentFulfillmentObligation],
    remote_action_ids: set[str],
) -> CommentOutcomeSnapshot:
    payload = {
        "plan_id": plan.id,
        "contract_state": plan.contract_state,
        "events": [_event_identity(row) for row in events],
        "obligations": [_obligation_identity(row) for row in obligations],
    }
    remote_started = any(_remote_started(row, remote_action_ids) for row in obligations)
    reconcile_open = any(_reconcile_open(row, remote_action_ids) for row in obligations)
    return CommentOutcomeSnapshot(
        plan_id=plan.id,
        state_hash=_hash(payload),
        terminal_state=plan.contract_state,
        remote_started=remote_started,
        reconcile_state="open" if reconcile_open else "closed",
    )


def _events_by_plan(
    session: Session,
    plan_ids: list[str],
) -> dict[str, list[ChannelCommentPlanLifecycleEvent]]:
    rows = session.scalars(select(ChannelCommentPlanLifecycleEvent).where(
        ChannelCommentPlanLifecycleEvent.plan_contract_id.in_(plan_ids),
    ).order_by(
        ChannelCommentPlanLifecycleEvent.plan_contract_id,
        ChannelCommentPlanLifecycleEvent.lifecycle_epoch,
        ChannelCommentPlanLifecycleEvent.id,
    ))
    grouped: dict[str, list[ChannelCommentPlanLifecycleEvent]] = {}
    for row in rows:
        grouped.setdefault(row.plan_contract_id, []).append(row)
    return grouped


def _obligations_by_plan(
    session: Session,
    plan_ids: list[str],
) -> dict[str, list[CommentFulfillmentObligation]]:
    rows = session.scalars(select(CommentFulfillmentObligation).where(
        CommentFulfillmentObligation.plan_contract_id.in_(plan_ids),
    ).order_by(
        CommentFulfillmentObligation.plan_contract_id,
        CommentFulfillmentObligation.target_ordinal,
    ))
    grouped: dict[str, list[CommentFulfillmentObligation]] = {}
    for row in rows:
        grouped.setdefault(row.plan_contract_id, []).append(row)
    return grouped


def _remote_action_ids(session: Session, action_ids: list[str | None]) -> set[str]:
    ids = sorted({value for value in action_ids if value})
    if not ids:
        return set()
    remote = set(session.scalars(select(Action.id).where(
        Action.id.in_(ids),
        Action.status.in_(("success", "unknown_after_send")),
    )))
    remote.update(session.scalars(select(ExecutionAttempt.action_id).where(
        ExecutionAttempt.action_id.in_(ids),
        ExecutionAttempt.gateway_call_started_at.is_not(None),
    )))
    return remote


def _event_identity(row: ChannelCommentPlanLifecycleEvent) -> dict:
    return {
        "epoch": row.lifecycle_epoch,
        "type": row.event_type,
        "evidence_hash": row.evidence_hash,
        "result_hash": row.result_hash,
        "state": row.event_state,
    }


def _obligation_identity(row: CommentFulfillmentObligation) -> dict:
    return {
        "ordinal": row.target_ordinal,
        "status": row.status,
        "action_id": row.current_action_id or "",
        "remote_comment_id_hash": _hash(row.remote_comment_id or ""),
    }


def _remote_started(
    row: CommentFulfillmentObligation,
    remote_action_ids: set[str],
) -> bool:
    return bool(
        row.remote_comment_id
        or row.remote_confirmed_at
        or row.status in {"confirmed", "unknown"}
        or row.current_action_id in remote_action_ids
    )


def _reconcile_open(
    row: CommentFulfillmentObligation,
    remote_action_ids: set[str],
) -> bool:
    return bool(
        row.status == "unknown"
        or (
            row.current_action_id in remote_action_ids
            and not row.remote_comment_id
            and row.remote_confirmed_at is None
        )
    )


def _tombstone_value(task: Task, snapshot: CommentOutcomeSnapshot) -> dict:
    mutation_hash = _hash([COMMENT_LIFECYCLE_MUTATION, task.id, snapshot.plan_id])
    request_hash = _hash([snapshot.plan_id, snapshot.state_hash])
    return {
        "tenant_id": task.tenant_id,
        "original_task_id": task.id,
        "mutation_kind": COMMENT_LIFECYCLE_MUTATION,
        "remote_mutation_key_hash": mutation_hash,
        "gateway_request_hash": request_hash,
        "remote_started": snapshot.remote_started,
        "terminal_state": snapshot.terminal_state,
        "remote_fact_identity_hash": snapshot.state_hash,
        "reconcile_state": snapshot.reconcile_state,
    }


def _hash(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


__all__ = [
    "COMMENT_OUTCOME_ITEM",
    "comment_outcome_snapshots",
    "comment_tombstone_values",
]

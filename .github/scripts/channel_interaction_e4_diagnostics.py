from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, text

from app.database import SessionLocal
from app.models import (
    AccountPacingReservation,
    Action,
    CommentFulfillmentObligation,
    ExecutionAttempt,
    ReactionFulfillmentObligation,
    ReactionRemoteFact,
    Task,
    TaskDayLedger,
    ViewFulfillmentObligation,
    ViewRemoteFact,
)


BEIJING = ZoneInfo("Asia/Shanghai")
RELEASE_LIVE_AT_ENV = "TASK_FULFILLMENT_RELEASE_LIVE_AT"
TASK_IDS_ENV = "TASK_FULFILLMENT_E4_TASK_IDS"
TASK_TYPES = frozenset({"channel_comment", "channel_like", "channel_view"})
ACTION_TYPES = {
    "channel_comment": "post_comment",
    "channel_like": "like_message",
    "channel_view": "view_message",
}
OPEN_ACTION_STATUSES = frozenset({"pending", "claiming", "executing"})
NON_REQUIRED_OBLIGATION_STATUSES = frozenset({"closed_expired"})


LIFECYCLE_MISMATCH_QUERY = text("""
    SELECT action.id AS action_id, action.task_id, task.name AS task_name,
           action.task_lifecycle_epoch AS action_epoch,
           task.task_lifecycle_epoch AS task_epoch,
           action.status AS action_status, action.scheduled_at,
           obligation.id AS obligation_id,
           obligation.status AS obligation_status,
           obligation.current_action_id,
           current_action.status AS current_action_status,
           current_action.task_lifecycle_epoch AS current_action_epoch,
           reservation.state AS reservation_state,
           message.created_at AS source_observed_at,
           message.published_at AS source_published_at
    FROM actions AS action
    JOIN tasks AS task ON task.id = action.task_id
    JOIN reaction_fulfillment_obligations AS obligation
      ON obligation.id = action.payload ->> 'reaction_fulfillment_obligation_id'
    LEFT JOIN actions AS current_action
      ON current_action.id = obligation.current_action_id
    LEFT JOIN account_pacing_reservations AS reservation
      ON reservation.action_id = action.id
    LEFT JOIN channel_messages AS message
      ON message.id = obligation.channel_message_id
    WHERE task.type = 'channel_like'
      AND task.status = 'running'
      AND task.fulfillment_contract_version = 'fact_first_v3'
      AND action.action_type = 'like_message'
      AND action.status IN ('pending', 'claiming', 'executing')
      AND action.task_lifecycle_epoch <> task.task_lifecycle_epoch
    ORDER BY action.scheduled_at, action.id
    LIMIT 30
""")


def _release_since() -> datetime:
    raw = os.environ[RELEASE_LIVE_AT_ENV].strip()
    value = datetime.fromisoformat(raw)
    return value.replace(tzinfo=BEIJING) if value.tzinfo is None else value.astimezone(BEIJING)


def _task_ids_from_env() -> list[str]:
    raw = os.getenv(TASK_IDS_ENV, "")
    return list(dict.fromkeys(item.strip() for item in raw.split(",") if item.strip()))


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _json_row(row) -> dict[str, Any]:
    return {
        key: value.isoformat() if isinstance(value, datetime) else value
        for key, value in row.items()
    }


def _status_counts(session, model, scope) -> dict[str, int]:
    rows = session.execute(
        select(model.status, func.count(model.id)).where(scope).group_by(model.status)
    )
    return {str(status): int(count) for status, count in rows}


def _due_counts(session, model, scope, now: datetime) -> dict[str, int]:
    required_scope = model.status.notin_(NON_REQUIRED_OBLIGATION_STATUSES)
    due_scope = (required_scope, model.pacing_due_at.is_not(None), model.pacing_due_at <= now)
    due = int(session.scalar(select(func.count(model.id)).where(scope, *due_scope)) or 0)
    confirmed = int(session.scalar(select(func.count(model.id)).where(
        scope, *due_scope, model.status == "confirmed",
    )) or 0)
    missing_due = int(session.scalar(select(func.count(model.id)).where(
        scope, required_scope, model.pacing_due_at.is_(None),
    )) or 0)
    return {"due": due, "due_confirmed": confirmed, "due_at_missing": missing_due}


def _comment_obligations(session, task: Task, since: datetime, now: datetime) -> dict[str, Any]:
    model = CommentFulfillmentObligation
    scope = model.task_id == task.id
    post_release = int(session.scalar(select(func.count(model.id)).where(
        scope,
        model.status == "confirmed",
        model.remote_comment_id.is_not(None),
        model.remote_comment_id != "",
        model.remote_confirmed_at >= since,
    )) or 0)
    return {
        "status_counts": _status_counts(session, model, scope),
        **_due_counts(session, model, scope, now),
        "post_release_remote_fact_count": post_release,
    }


def _reaction_obligations(session, task: Task, since: datetime, now: datetime) -> dict[str, Any]:
    model = ReactionFulfillmentObligation
    scope = model.task_id == task.id
    post_release = int(session.scalar(
        select(func.count(ReactionRemoteFact.id))
        .join(model, model.id == ReactionRemoteFact.obligation_id)
        .where(scope, ReactionRemoteFact.remote_confirmed_at >= since)
    ) or 0)
    return {
        "status_counts": _status_counts(session, model, scope),
        **_due_counts(session, model, scope, now),
        "post_release_remote_fact_count": post_release,
    }


def _view_obligations(session, task: Task, since: datetime, now: datetime) -> dict[str, Any]:
    model = ViewFulfillmentObligation
    ledger_ids = list(session.scalars(
        select(TaskDayLedger.id)
        .where(TaskDayLedger.task_id == task.id)
    ))
    scope = model.task_day_ledger_id.in_(ledger_ids) if ledger_ids else model.task_day_ledger_id.is_(None)
    post_release = int(session.scalar(
        select(func.count(ViewRemoteFact.id))
        .join(model, model.id == ViewRemoteFact.obligation_id)
        .where(scope, ViewRemoteFact.remote_confirmed_at >= since)
    ) or 0)
    total_remote_facts = int(session.scalar(
        select(func.count(ViewRemoteFact.id))
        .join(model, model.id == ViewRemoteFact.obligation_id)
        .where(scope)
    ) or 0)
    today_remote_facts = int(session.scalar(
        select(func.count(ViewRemoteFact.id))
        .join(model, model.id == ViewRemoteFact.obligation_id)
        .where(scope, ViewRemoteFact.obligation_local_date == now.date())
    ) or 0)
    latest_confirmed = session.scalar(
        select(func.max(ViewRemoteFact.remote_confirmed_at))
        .join(model, model.id == ViewRemoteFact.obligation_id)
        .where(scope)
    )
    recent_facts = session.execute(
        select(
            ViewRemoteFact.account_id,
            ViewRemoteFact.channel_message_id,
            ViewRemoteFact.remote_confirmed_at,
        )
        .join(model, model.id == ViewRemoteFact.obligation_id)
        .where(scope)
        .order_by(ViewRemoteFact.remote_confirmed_at.desc().nullslast())
        .limit(3)
    ).mappings()
    return {
        "status_counts": _status_counts(session, model, scope),
        **_due_counts(session, model, scope, now),
        "post_release_remote_fact_count": post_release,
        "total_remote_facts": total_remote_facts,
        "today_remote_facts": today_remote_facts,
        "latest_confirmed_at": _iso(latest_confirmed),
        "recent_facts_samples": [_json_row(row) for row in recent_facts],
    }


def _action_snapshot(session, task: Task, since: datetime, now: datetime) -> dict[str, Any]:
    action_type = ACTION_TYPES[task.type]
    scope = _action_scope(session, task, action_type)
    rows = session.execute(
        select(Action.status, func.count(Action.id)).where(*scope).group_by(Action.status)
    )
    status_counts = {str(status): int(count) for status, count in rows}
    due_open = int(session.scalar(select(func.count(Action.id)).where(
        *scope,
        Action.status.in_(OPEN_ACTION_STATUSES),
        Action.scheduled_at <= now,
    )) or 0)
    post_release = int(session.scalar(select(func.count(Action.id)).where(
        *scope, Action.created_at >= since,
    )) or 0)
    oldest_due = session.scalar(select(func.min(Action.scheduled_at)).where(
        *scope,
        Action.status.in_(OPEN_ACTION_STATUSES),
        Action.scheduled_at <= now,
    ))
    return {
        "status_counts": status_counts,
        "due_open_count": due_open,
        "post_release_created_count": post_release,
        "oldest_due_open_at": _iso(oldest_due),
        **_claimability_snapshot(session, task, action_type, now),
    }


def _action_scope(session, task: Task, action_type: str) -> tuple:
    return (Action.task_id == task.id, Action.action_type == action_type)


def _claimability_snapshot(session, task: Task, action_type: str, now: datetime) -> dict[str, int]:
    reservation = select(AccountPacingReservation.id).where(
        AccountPacingReservation.tenant_id == Action.tenant_id,
        AccountPacingReservation.account_id == Action.account_id,
        AccountPacingReservation.pacing_slot_key == Action.pacing_slot_key,
        AccountPacingReservation.state.in_(("reserved", "bound")),
    ).exists()
    scope = (
        *_action_scope(session, task, action_type),
        Action.status.in_(OPEN_ACTION_STATUSES),
        Action.scheduled_at <= now,
        Action.task_lifecycle_epoch == task.task_lifecycle_epoch,
    )
    claimable = int(session.scalar(select(func.count(Action.id)).where(
        *scope,
        (Action.pacing_slot_key.is_(None))
        | (Action.pacing_slot_key == "")
        | (Action.account_id.is_(None))
        | reservation,
    )) or 0)
    lifecycle_mismatch = int(session.scalar(select(func.count(Action.id)).where(
        *_action_scope(session, task, action_type),
        Action.status.in_(OPEN_ACTION_STATUSES),
        Action.scheduled_at <= now,
        Action.task_lifecycle_epoch != task.task_lifecycle_epoch,
    )) or 0)
    return {
        "due_direct_claimable_count": claimable,
        "due_missing_reservation_count": max(0, _due_open_count(session, scope) - claimable),
        "due_lifecycle_mismatch_count": lifecycle_mismatch,
    }


def _due_open_count(session, scope: tuple) -> int:
    return int(session.scalar(select(func.count(Action.id)).where(*scope)) or 0)


def _attempt_snapshot(session, task: Task, since: datetime) -> dict[str, Any]:
    action_type = ACTION_TYPES[task.type]
    observed_at = func.coalesce(ExecutionAttempt.after_call_at, ExecutionAttempt.created_at)
    scope = (
        Action.task_id == task.id,
        Action.action_type == action_type,
        observed_at >= since,
    )
    rows = session.execute(
        select(ExecutionAttempt.status, func.count(ExecutionAttempt.id))
        .join(Action, Action.id == ExecutionAttempt.action_id)
        .where(*scope)
        .group_by(ExecutionAttempt.status)
    )
    counts = {str(status): int(count) for status, count in rows}
    gateway = int(session.scalar(
        select(func.count(ExecutionAttempt.id))
        .join(Action, Action.id == ExecutionAttempt.action_id)
        .where(*scope, ExecutionAttempt.gateway_call_started_at.is_not(None))
    ) or 0)
    return {
        "post_release_status_counts": counts,
        **_attempt_failure_snapshot(session, scope),
        "post_release_count": sum(counts.values()),
        "post_release_gateway_count": gateway,
    }


def _attempt_failure_snapshot(session, scope: tuple) -> dict[str, Any]:
    failure_rows = session.execute(
        select(ExecutionAttempt.failure_type, func.count(ExecutionAttempt.id))
        .join(Action, Action.id == ExecutionAttempt.action_id)
        .where(*scope, ExecutionAttempt.status == "failed")
        .group_by(ExecutionAttempt.failure_type)
    )
    counts = {str(failure_type or ""): int(count) for failure_type, count in failure_rows}
    samples = session.execute(
        select(
            ExecutionAttempt.id,
            ExecutionAttempt.action_id,
            Action.account_id,
            ExecutionAttempt.failure_type,
            ExecutionAttempt.gateway_call_started_at,
            ExecutionAttempt.after_call_at,
        )
        .join(Action, Action.id == ExecutionAttempt.action_id)
        .where(*scope, ExecutionAttempt.status == "failed")
        .order_by(ExecutionAttempt.created_at.desc())
        .limit(5)
    ).mappings()
    return {
        "post_release_failure_type_counts": counts,
        "post_release_failure_samples": [_json_row(row) for row in samples],
    }


def _obligation_snapshot(session, task: Task, since: datetime, now: datetime) -> dict[str, Any]:
    if task.type == "channel_comment":
        return _comment_obligations(session, task, since, now)
    if task.type == "channel_like":
        return _reaction_obligations(session, task, since, now)
    return _view_obligations(session, task, since, now)


def _blockers(
    task: Task,
    *,
    obligations: dict[str, Any],
    actions: dict[str, Any],
    attempts: dict[str, Any],
) -> list[str]:
    blockers: list[str] = []
    if task.status != "running":
        blockers.append("task_not_running")
    status_counts = obligations["status_counts"]
    if sum(status_counts.values()) == 0:
        blockers.append("interaction_obligation_missing")
    if _expired_only_without_remote_fact(obligations):
        blockers.append("interaction_expired_unmet")
    if obligations["due_confirmed"] < obligations["due"]:
        blockers.append("interaction_due_unmet")
    if actions["due_lifecycle_mismatch_count"]:
        blockers.append("interaction_lifecycle_mismatch")
    if actions["due_open_count"] and not attempts["post_release_count"]:
        blockers.append("interaction_dispatch_stalled")
    if (
        obligations["due_confirmed"] < obligations["due"]
        and not obligations["post_release_remote_fact_count"]
    ):
        blockers.append("interaction_post_release_remote_fact_missing")
    return blockers


def _expired_only_without_remote_fact(obligations: dict[str, Any]) -> bool:
    status_counts = obligations["status_counts"]
    expired = int(status_counts.get("closed_expired") or 0)
    required = sum(
        int(count)
        for status, count in status_counts.items()
        if status not in NON_REQUIRED_OBLIGATION_STATUSES
    )
    return (
        expired > 0
        and required == 0
        and int(obligations["post_release_remote_fact_count"] or 0) == 0
    )


def _goal_status(task: Task, blockers: list[str]) -> str:
    if task.status == "paused":
        return "paused"
    if "interaction_expired_unmet" in blockers:
        return "missed"
    return "met" if not blockers else "not_met"


def _snapshot(
    session,
    task: Task,
    *,
    since: datetime,
    now: datetime,
) -> dict[str, Any]:
    obligations = _obligation_snapshot(session, task, since, now)
    actions = _action_snapshot(session, task, since, now)
    attempts = _attempt_snapshot(session, task, since)
    blockers = _blockers(
        task, obligations=obligations, actions=actions, attempts=attempts,
    )
    return {
        "task_id": task.id,
        "task_name": task.name,
        "task_type": task.type,
        "task_status": task.status,
        "contract": task.fulfillment_contract_version,
        "next_run_at": _iso(task.next_run_at),
        "last_error": str(task.last_error or "")[:240],
        "type_config": dict(task.type_config or {}),
        "pacing_config": dict(task.pacing_config or {}),
        "obligations": obligations,
        "actions": actions,
        "attempts": attempts,
        "blockers": blockers,
        "goal_status": _goal_status(task, blockers),
    }


def main() -> None:
    since = _release_since()
    now = datetime.now(BEIJING)
    failed = False
    with SessionLocal() as session:
        task_ids = _task_ids_from_env()
        scope = [
            Task.type.in_(TASK_TYPES),
            Task.status.in_(("running", "paused", "completed")),
            Task.deleted_at.is_(None),
        ]
        if task_ids:
            scope.append(Task.id.in_(task_ids))
        tasks = list(session.scalars(
            select(Task).where(*scope).order_by(Task.type, Task.name, Task.id)
        ))
        print("CHANNEL_INTERACTION_TASK_COUNT=" + str(len(tasks)), flush=True)
        for task in tasks:
            snapshot = _snapshot(session, task, since=since, now=now)
            failed = failed or snapshot["goal_status"] not in {"met", "paused"}
            print(
                "CHANNEL_INTERACTION_E4="
                + json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
                flush=True,
            )
        mismatch_rows = session.execute(LIFECYCLE_MISMATCH_QUERY).mappings()
        for row in mismatch_rows:
            print(
                "CHANNEL_INTERACTION_LIFECYCLE_MISMATCH="
                + json.dumps(_json_row(row), ensure_ascii=False, sort_keys=True),
                flush=True,
            )
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

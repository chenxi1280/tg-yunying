from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from app.database import SessionLocal
from app.models import (
    Action,
    ExecutionAttempt,
    GatewayRequestEvidenceJournal,
    RemoteReconcileCase,
    Task,
    WorkerHeartbeat,
)
from app.services.task_center.service import _apply_type_config_data
from app.services.task_center.runtime_state_hash import (
    execution_attempt_state_hash,
    remote_reconcile_action_state_hash,
)


ACTIVE_STATUSES = ("pending", "running")
APPLY_ENV = "REPLY_RATIO_APPLY"
COMMENT_PERCENT_ENV = "REPLY_RATIO_COMMENT_PERCENT"
AI_PERCENT_ENV = "REPLY_RATIO_AI_PERCENT"
SINCE_EPOCH_ENV = "REPLY_RATIO_SINCE_EPOCH"
DEFAULT_LOOKBACK_HOURS = 24
COMMENT_ACTION = "post_comment"
AI_ACTION = "send_message"
OPEN_ACTION_STATUSES = ("pending", "claiming", "executing")
TERMINAL_DIAGNOSTIC_STATUSES = ("failed", "skipped")
TERMINAL_DIAGNOSTIC_LIMIT = 10


@dataclass(frozen=True)
class RatioTarget:
    task_type: str
    total_field: str
    reply_field: str
    percent: int
    action_type: str


def parse_percent(name: str) -> int:
    raw = os.getenv(name, "").strip()
    if not raw.isdigit():
        raise ValueError(f"{name} must be an integer from 0 to 100")
    value = int(raw)
    if value > 100:
        raise ValueError(f"{name} must be an integer from 0 to 100")
    return value


def parse_apply() -> bool:
    raw = os.getenv(APPLY_ENV, "false").strip().lower()
    if raw not in {"true", "false"}:
        raise ValueError(f"{APPLY_ENV} must be true or false")
    return raw == "true"


def parse_since() -> datetime:
    raw = os.getenv(SINCE_EPOCH_ENV, "0").strip()
    if not raw.isdigit():
        raise ValueError(f"{SINCE_EPOCH_ENV} must be a non-negative epoch")
    epoch = int(raw)
    if epoch:
        return datetime.fromtimestamp(epoch, tz=UTC)
    return datetime.now(tz=UTC) - timedelta(hours=DEFAULT_LOOKBACK_HOURS)


def reply_minimum(total: int, percent: int) -> int:
    if total <= 0:
        raise ValueError("reply ratio denominator must be positive")
    if percent < 0 or percent > 100:
        raise ValueError("reply percent must be from 0 to 100")
    return (total * percent + 99) // 100


def targets() -> dict[str, RatioTarget]:
    return {
        "channel_comment": RatioTarget(
            "channel_comment", "target_comments_per_message", "reply_min_per_message",
            parse_percent(COMMENT_PERCENT_ENV), COMMENT_ACTION,
        ),
        "group_ai_chat": RatioTarget(
            "group_ai_chat", "messages_per_round", "reply_min_per_round",
            parse_percent(AI_PERCENT_ENV), AI_ACTION,
        ),
    }


def task_change(task: Task, target: RatioTarget) -> dict[str, Any]:
    config = dict(task.type_config or {})
    total = int(config.get(target.total_field) or 0)
    desired = reply_minimum(total, target.percent)
    change: dict[str, Any] = {}
    if int(config.get(target.reply_field) or 0) != desired:
        change[target.reply_field] = desired
    if task.type == "channel_comment" and config.get("comment_mode") != "mixed":
        change["comment_mode"] = "mixed"
    return change


def task_snapshot(task: Task, target: RatioTarget) -> dict[str, Any]:
    config = dict(task.type_config or {})
    total = int(config.get(target.total_field) or 0)
    desired = reply_minimum(total, target.percent)
    return {
        "task_id": task.id,
        "task_name": task.name,
        "task_type": task.type,
        "status": task.status,
        "config_revision": task.config_revision,
        "total_field": target.total_field,
        "total": total,
        "target_percent": target.percent,
        "reply_field": target.reply_field,
        "current_reply_minimum": int(config.get(target.reply_field) or 0),
        "desired_reply_minimum": desired,
        "comment_mode": config.get("comment_mode") if task.type == "channel_comment" else None,
        "next_run_at": task.next_run_at.isoformat() if task.next_run_at else None,
        "needs_change": bool(task_change(task, target)),
    }


def reply_fact_snapshot(session, task: Task, target: RatioTarget, since: datetime) -> dict[str, Any]:
    rows = session.execute(
        select(Action, ExecutionAttempt)
        .join(ExecutionAttempt, ExecutionAttempt.action_id == Action.id)
        .where(
            Action.task_id == task.id,
            Action.action_type == target.action_type,
            ExecutionAttempt.status == "success",
            ExecutionAttempt.remote_message_id != "",
            ExecutionAttempt.after_call_at >= since,
        )
        .order_by(ExecutionAttempt.after_call_at.desc())
    )
    facts: dict[str, tuple[Action, ExecutionAttempt]] = {}
    for action, attempt in rows:
        facts.setdefault(action.id, (action, attempt))
    reply_rows = [pair for pair in facts.values() if (pair[0].payload or {}).get("reply_to_message_id")]
    return {
        "since": since.isoformat(),
        "remote_success_count": len(facts),
        "remote_reply_success_count": len(reply_rows),
        "remote_direct_success_count": len(facts) - len(reply_rows),
        "latest_reply_samples": [reply_sample(*pair) for pair in reply_rows[:5]],
    }


def reply_sample(action: Action, attempt: ExecutionAttempt) -> dict[str, Any]:
    return {
        "action_id": action.id,
        "reply_to_message_id": (action.payload or {}).get("reply_to_message_id"),
        "remote_message_id": attempt.remote_message_id,
        "after_call_at": attempt.after_call_at.isoformat() if attempt.after_call_at else None,
    }


def open_action_snapshot(session, task: Task, target: RatioTarget) -> dict[str, Any]:
    actions = list(session.scalars(
        select(Action).where(
            Action.task_id == task.id,
            Action.action_type == target.action_type,
            Action.status.in_(OPEN_ACTION_STATUSES),
        )
    ))
    reply_count = sum(bool((action.payload or {}).get("reply_to_message_id")) for action in actions)
    scheduled = sorted(action.scheduled_at for action in actions if action.scheduled_at)
    return {
        "open_count": len(actions),
        "open_reply_count": reply_count,
        "open_direct_count": len(actions) - reply_count,
        "earliest_open_scheduled_at": scheduled[0].isoformat() if scheduled else None,
        "latest_open_scheduled_at": scheduled[-1].isoformat() if scheduled else None,
    }


def terminal_action_snapshot(
    session,
    task: Task,
    target: RatioTarget,
    since: datetime,
) -> list[dict[str, Any]]:
    actions = list(session.scalars(
        select(Action)
        .where(
            Action.task_id == task.id,
            Action.action_type == target.action_type,
            Action.status.in_(TERMINAL_DIAGNOSTIC_STATUSES),
            Action.scheduled_at >= since,
        )
        .order_by(Action.executed_at.desc().nullslast(), Action.scheduled_at.desc())
        .limit(TERMINAL_DIAGNOSTIC_LIMIT)
    ))
    return [_terminal_action_row(action, _latest_attempt(session, action)) for action in actions]


def _latest_attempt(session, action: Action) -> ExecutionAttempt | None:
    return session.scalar(
        select(ExecutionAttempt)
        .where(ExecutionAttempt.action_id == action.id)
        .order_by(ExecutionAttempt.attempt_no.desc())
        .limit(1)
    )


def _terminal_action_row(
    action: Action,
    attempt: ExecutionAttempt | None,
) -> dict[str, Any]:
    payload = action.payload if isinstance(action.payload, dict) else {}
    return {
        "action_id": action.id,
        "action_status": action.status,
        "account_id": action.account_id,
        "scheduled_at": action.scheduled_at.isoformat() if action.scheduled_at else None,
        "executed_at": action.executed_at.isoformat() if action.executed_at else None,
        "reply_to_message_id": payload.get("reply_to_message_id"),
        "ai_generation_status": payload.get("ai_generation_status"),
        "result_contract": _remote_result_contract(action),
        "attempt_status": attempt.status if attempt else None,
        "attempt_failure_type": attempt.failure_type if attempt else None,
        "attempt_remote_message_id": attempt.remote_message_id if attempt else None,
        "gateway_call_started_at": (
            attempt.gateway_call_started_at.isoformat()
            if attempt and attempt.gateway_call_started_at else None
        ),
        "after_call_at": (
            attempt.after_call_at.isoformat() if attempt and attempt.after_call_at else None
        ),
    }


def unknown_remote_snapshot(
    session,
    task: Task,
    target: RatioTarget,
    since: datetime,
) -> list[dict[str, Any]]:
    actions = list(session.scalars(
        select(Action)
        .where(
            Action.task_id == task.id,
            Action.action_type == target.action_type,
            Action.status == "unknown_after_send",
            Action.executed_at >= since,
        )
        .order_by(Action.executed_at.desc())
        .limit(10)
    ))
    return [_unknown_remote_row(session, action) for action in actions]


def _unknown_remote_row(session, action: Action) -> dict[str, Any]:
    attempt = session.scalar(
        select(ExecutionAttempt)
        .where(ExecutionAttempt.action_id == action.id)
        .order_by(ExecutionAttempt.attempt_no.desc())
        .limit(1)
    )
    case = session.scalar(
        select(RemoteReconcileCase)
        .where(RemoteReconcileCase.action_id == action.id)
        .order_by(RemoteReconcileCase.created_at.desc())
        .limit(1)
    )
    journal = session.scalar(
        select(GatewayRequestEvidenceJournal)
        .where(GatewayRequestEvidenceJournal.action_id == action.id)
        .order_by(GatewayRequestEvidenceJournal.observed_at.desc())
        .limit(1)
    )
    expected_action_hash = case.expected_action_state_hash if case else ""
    current_action_hash = remote_reconcile_action_state_hash(action)
    expected_attempt_hash = case.expected_attempt_state_hash if case else ""
    current_attempt_hash = execution_attempt_state_hash(attempt) if attempt else ""
    return {
        "action_id": action.id,
        "action_status": action.status,
        "account_id": action.account_id,
        "scheduled_at": action.scheduled_at.isoformat() if action.scheduled_at else None,
        "executed_at": action.executed_at.isoformat() if action.executed_at else None,
        "retry_count": action.retry_count,
        "primary_quantity_slot_id": action.primary_quantity_slot_id,
        "content_mix_cycle_slot_id": action.content_mix_cycle_slot_id,
        "content_mix_slot_attempt": action.content_mix_slot_attempt,
        "result_contract": _remote_result_contract(action),
        "attempt_id": attempt.id if attempt else None,
        "attempt_status": attempt.status if attempt else None,
        "attempt_failure_sql": _failure_sql(attempt),
        "case_id": case.id if case else None,
        "case_state": case.state if case else None,
        "case_expected_action_state_hash_b64": _hash_b64(expected_action_hash),
        "current_action_state_hash_b64": _hash_b64(current_action_hash),
        "case_expected_attempt_state_hash_b64": _hash_b64(expected_attempt_hash),
        "current_attempt_state_hash_b64": _hash_b64(current_attempt_hash),
        "claim_owner": action.claim_owner,
        "claim_expires_at": (
            action.claim_expires_at.isoformat() if action.claim_expires_at else None
        ),
        "lease_owner": action.lease_owner,
        "lease_expires_at": (
            action.lease_expires_at.isoformat() if action.lease_expires_at else None
        ),
        "journal_state": journal.state if journal else None,
        "journal_remote_mutation_state": (
            journal.remote_mutation_state if journal else None
        ),
        "journal_remote_message_id": journal.remote_message_id if journal else None,
        "journal_failure_code": journal.failure_code if journal else None,
    }


def _hash_b64(value: str) -> str:
    return base64.b64encode(bytes.fromhex(value)).decode() if value else ""


def _remote_result_contract(action: Action) -> dict[str, Any]:
    result = action.result if isinstance(action.result, dict) else {}
    keys = (
        "dispatch_claim_active",
        "dispatch_claim_scope",
        "dispatch_claim_window_id",
        "dispatch_claim_shard_allocation_id",
        "dispatch_reservation_id",
        "gateway_call_state",
        "gateway_call_started_at",
        "gateway_request_id",
        "gateway_request_identity",
        "gateway_request_fingerprint",
        "gateway_target_fingerprint",
        "remote_message_id",
        "remote_fact_id",
        "telegram_msg_id",
        "error_code",
    )
    return {key: result[key] for key in keys if key in result}


def _failure_sql(attempt: ExecutionAttempt | None) -> str:
    detail = str(attempt.failure_detail or "") if attempt else ""
    if "[SQL:" not in detail:
        return ""
    statement = detail.split("[SQL:", 1)[1].split("]", 1)[0]
    return " ".join(statement.split())[:500]


def runtime_snapshot(session, task_rows: list[Task]) -> dict[str, Any]:
    now = datetime.now(tz=UTC)
    due_ids = list(session.scalars(
        select(Task.id)
        .where(Task.status == "running", (Task.next_run_at.is_(None)) | (Task.next_run_at <= now))
        .order_by(Task.priority.asc(), Task.next_run_at.asc().nullsfirst(), Task.created_at.asc())
    ))
    due_ranks = {task_id: index for index, task_id in enumerate(due_ids, start=1)}
    heartbeats = list(session.scalars(
        select(WorkerHeartbeat)
        .where(WorkerHeartbeat.process_type == "planner")
        .order_by(WorkerHeartbeat.last_seen_at.desc())
        .limit(4)
    ))
    return {
        "observed_at": now.isoformat(),
        "planner_due_task_count": len(due_ids),
        "target_planner_due_ranks": {
            task.id: due_ranks.get(task.id)
            for task in task_rows
        },
        "planner_heartbeats": [
            {
                "worker_id": row.worker_id,
                "status": row.status,
                "last_seen_at": row.last_seen_at.isoformat(),
                "metadata": row.heartbeat_metadata or {},
            }
            for row in heartbeats
        ],
    }


def active_tasks(session, lock: bool) -> list[Task]:
    statement = (
        select(Task)
        .where(
            Task.deleted_at.is_(None),
            Task.status.in_(ACTIVE_STATUSES),
            Task.type.in_(("channel_comment", "group_ai_chat")),
        )
        .order_by(Task.type, Task.id)
    )
    if lock:
        statement = statement.with_for_update()
    return list(session.scalars(statement))


def emit(label: str, payload: Any) -> None:
    print(f"{label}=" + json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)


def snapshots(
    session,
    task_rows: list[Task],
    target_map: dict[str, RatioTarget],
    since: datetime,
) -> list[dict[str, Any]]:
    result = []
    for task in task_rows:
        target = target_map[task.type]
        result.append({
            **task_snapshot(task, target),
            **open_action_snapshot(session, task, target),
            **reply_fact_snapshot(session, task, target, since),
            "latest_terminal_rows": terminal_action_snapshot(
                session, task, target, since,
            ),
            "unknown_remote_rows": unknown_remote_snapshot(
                session, task, target, since,
            ),
        })
    return result


def apply_changes(session, task_rows: list[Task], target_map: dict[str, RatioTarget]) -> list[dict[str, Any]]:
    changes = []
    for task in task_rows:
        update_data = task_change(task, target_map[task.type])
        if not update_data:
            continue
        changes.append({"task_id": task.id, "task_type": task.type, "update": update_data})
        _apply_type_config_data(
            session, task.tenant_id, task.id, task.type, update_data,
            actor="production-reply-ratio-control",
        )
    session.commit()
    return changes


def main() -> None:
    apply = parse_apply()
    since = parse_since()
    target_map = targets()
    with SessionLocal() as session:
        task_rows = active_tasks(session, lock=apply)
        if not task_rows:
            raise RuntimeError("no active channel_comment or group_ai_chat tasks found")
        before = snapshots(session, task_rows, target_map, since)
        emit("REPLY_RATIO_BEFORE", before)
        changes = apply_changes(session, task_rows, target_map) if apply else []
        session.expire_all()
        after_rows = active_tasks(session, lock=False)
        after = snapshots(session, after_rows, target_map, since)
        emit("REPLY_RATIO_CHANGES", changes)
        emit("REPLY_RATIO_AFTER", after)
        emit("REPLY_RATIO_RUNTIME", runtime_snapshot(session, after_rows))
        summary = {
            "apply": apply,
            "active_task_count": len(after_rows),
            "changed_task_count": len(changes),
            "remaining_mismatch_count": sum(row["needs_change"] for row in after),
            "observed_at": datetime.now(tz=UTC).isoformat(),
            "observed_epoch": int(datetime.now(tz=UTC).timestamp()),
        }
        emit("REPLY_RATIO_SUMMARY", summary)
        if apply and summary["remaining_mismatch_count"]:
            raise RuntimeError("reply ratio update did not converge")


if __name__ == "__main__":
    main()

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import timedelta

from sqlalchemy import select

from app.database import SessionLocal
from app.models import Action, ChannelMessage, OperationTarget, Task
from app.services._common import _now


SINCE = _now() - timedelta(days=7)
ACTION_SCAN_LIMIT = 20000
TOP_LIMIT = 20
STATUS_COUNTED_BY_PLANNER = {"pending", "executing", "success", "failed"}


def iso(value) -> str | None:
    return value.isoformat() if value else None


def payload_int(payload: dict, key: str) -> int:
    raw = payload.get(key)
    if isinstance(raw, int):
        return raw
    text = str(raw or "").strip()
    return int(text) if text.isdigit() else 0


def action_message_key(action: Action) -> tuple[int, int, int] | None:
    payload = action.payload if isinstance(action.payload, dict) else {}
    channel_target_id = payload_int(payload, "channel_target_id")
    channel_message_id = payload_int(payload, "channel_message_id")
    message_id = payload_int(payload, "message_id")
    if not channel_target_id or not (channel_message_id or message_id):
        return None
    return channel_target_id, channel_message_id, message_id


def code_would_count(action: Action, message: ChannelMessage | None) -> bool:
    if not message or action.status not in STATUS_COUNTED_BY_PLANNER:
        return False
    payload = action.payload if isinstance(action.payload, dict) else {}
    return payload.get("channel_message_id") == message.id or payload.get("message_id") == message.message_id


def normalized_would_count(action: Action, message: ChannelMessage | None) -> bool:
    if not message or action.status not in STATUS_COUNTED_BY_PLANNER:
        return False
    payload = action.payload if isinstance(action.payload, dict) else {}
    return payload_int(payload, "channel_message_id") == message.id or payload_int(payload, "message_id") == message.message_id


def payload_type_sample(action: Action) -> dict:
    payload = action.payload if isinstance(action.payload, dict) else {}
    return {
        "status": action.status,
        "channel_message_id": payload.get("channel_message_id"),
        "channel_message_id_type": type(payload.get("channel_message_id")).__name__,
        "message_id": payload.get("message_id"),
        "message_id_type": type(payload.get("message_id")).__name__,
    }


def task_config(task: Task | None) -> dict:
    config = task.type_config if task and isinstance(task.type_config, dict) else {}
    return {
        "name": task.name if task else "",
        "status": task.status if task else "",
        "target_comments_per_message": config.get("target_comments_per_message"),
        "comment_count_jitter": config.get("comment_count_jitter"),
        "message_scope": config.get("message_scope"),
        "message_count": config.get("message_count"),
        "deleted_at": iso(task.deleted_at) if task else None,
    }


def main() -> None:
    with SessionLocal() as session:
        actions = list(
            session.scalars(
                select(Action)
                .where(Action.action_type == "post_comment", Action.task_type == "channel_comment", Action.created_at >= SINCE)
                .order_by(Action.created_at.desc())
                .limit(ACTION_SCAN_LIMIT)
            )
        )
        tasks = {task.id: task for task in session.scalars(select(Task).where(Task.type == "channel_comment"))}
        grouped: dict[tuple[int, int, int], dict] = {}
        for action in actions:
            key = action_message_key(action)
            if key is None:
                continue
            bucket = grouped.setdefault(
                key,
                {
                    "total": 0,
                    "status_counts": Counter(),
                    "task_counts": Counter(),
                    "task_status_counts": defaultdict(Counter),
                    "first_created_at": iso(action.created_at),
                    "last_created_at": iso(action.created_at),
                    "last_executed_at": iso(action.executed_at),
                },
            )
            bucket["total"] += 1
            bucket["status_counts"][action.status] += 1
            bucket["task_counts"][action.task_id] += 1
            bucket["task_status_counts"][action.task_id][action.status] += 1
            created_at = iso(action.created_at)
            executed_at = iso(action.executed_at)
            if created_at and (not bucket["first_created_at"] or created_at < bucket["first_created_at"]):
                bucket["first_created_at"] = created_at
            if created_at and (not bucket["last_created_at"] or created_at > bucket["last_created_at"]):
                bucket["last_created_at"] = created_at
            if executed_at and (not bucket["last_executed_at"] or executed_at > bucket["last_executed_at"]):
                bucket["last_executed_at"] = executed_at

        overages = []
        for key, bucket in sorted(grouped.items(), key=lambda item: item[1]["total"], reverse=True)[:TOP_LIMIT]:
            channel_target_id, channel_message_id, message_id = key
            channel = session.get(OperationTarget, channel_target_id)
            message = session.get(ChannelMessage, channel_message_id) if channel_message_id else None
            related_actions = [
                action
                for action in actions
                if action_message_key(action) == key
            ]
            task_breakdown = []
            for task_id, count in bucket["task_counts"].most_common(8):
                task_breakdown.append(
                    {
                        "task_id": task_id,
                        "count": count,
                        "status_counts": dict(bucket["task_status_counts"][task_id]),
                        "config": task_config(tasks.get(task_id)),
                    }
                )
            overages.append(
                {
                    "channel_target_id": channel_target_id,
                    "channel_title": channel.title if channel else "",
                    "channel_message_id": channel_message_id,
                    "message_id": message.message_id if message else message_id,
                    "message_comment_available": bool(message.comment_available) if message else None,
                    "total_actions_7d": bucket["total"],
                    "planner_exact_count_7d": sum(1 for action in related_actions if code_would_count(action, message)),
                    "planner_normalized_count_7d": sum(1 for action in related_actions if normalized_would_count(action, message)),
                    "status_counts": dict(bucket["status_counts"]),
                    "task_count": len(bucket["task_counts"]),
                    "task_breakdown": task_breakdown,
                    "payload_type_samples": [payload_type_sample(action) for action in related_actions[:5]],
                    "first_created_at": bucket["first_created_at"],
                    "last_created_at": bucket["last_created_at"],
                    "last_executed_at": bucket["last_executed_at"],
                }
            )

        active_tasks = []
        for task in sorted(tasks.values(), key=lambda item: item.updated_at or item.created_at, reverse=True)[:20]:
            config = task.type_config if isinstance(task.type_config, dict) else {}
            active_tasks.append(
                {
                    "task_id": task.id,
                    "name": task.name,
                    "status": task.status,
                    "deleted": bool(task.deleted_at),
                    "updated_at": iso(task.updated_at),
                    "target_channel_id": config.get("target_channel_id"),
                    "target_comments_per_message": config.get("target_comments_per_message"),
                    "comment_count_jitter": config.get("comment_count_jitter"),
                    "message_scope": config.get("message_scope"),
                    "message_count": config.get("message_count"),
                }
            )
        output = {
            "scanned_actions": len(actions),
            "since": iso(SINCE),
            "overage_candidates": overages,
            "recent_channel_comment_tasks": active_tasks,
        }
        print("COMMENT_LIMIT_INVESTIGATION", json.dumps(output, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()

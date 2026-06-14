from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import timedelta

from sqlalchemy import func, select

from app.database import SessionLocal
from app.models import Action, ChannelMessage, ChannelMessageComment, OperationTarget, Task
from app.services._common import _now


SINCE = _now() - timedelta(hours=24)


def iso(value) -> str | None:
    return value.isoformat() if value else None


def short_action(action: Action) -> dict:
    payload = action.payload if isinstance(action.payload, dict) else {}
    result = action.result if isinstance(action.result, dict) else {}
    return {
        "id": action.id,
        "status": action.status,
        "created_at": iso(action.created_at),
        "scheduled_at": iso(action.scheduled_at),
        "executed_at": iso(action.executed_at),
        "account_id": action.account_id,
        "channel_message_id": payload.get("channel_message_id"),
        "message_id": payload.get("message_id"),
        "comment_mode": payload.get("comment_mode"),
        "reply_to_message_id": payload.get("reply_to_message_id"),
        "reply_target_source": payload.get("reply_target_source"),
        "reply_target_author": payload.get("reply_target_author"),
        "reply_target_preview": str(payload.get("reply_target_preview") or "")[:80],
        "failure_type": result.get("failure_type") or result.get("error_code"),
        "failure_detail": str(result.get("detail") or result.get("error_message") or result.get("error") or "")[:160],
        "telegram_msg_id": result.get("telegram_msg_id") or result.get("remote_message_id"),
    }


def channel_comment_stats(session, channel_target_id: int) -> dict:
    message_rows = list(
        session.execute(
            select(ChannelMessage.id, ChannelMessage.message_id, ChannelMessage.comment_available)
            .where(ChannelMessage.channel_target_id == channel_target_id)
            .order_by(ChannelMessage.id.desc())
            .limit(30)
        )
    )
    if not message_rows:
        return {"message_count_sample": 0, "comment_total": 0, "per_message_sample": []}
    message_ids = [row.id for row in message_rows]
    counts = dict(
        session.execute(
            select(ChannelMessageComment.channel_message_id, func.count(ChannelMessageComment.id))
            .where(ChannelMessageComment.channel_message_id.in_(message_ids))
            .group_by(ChannelMessageComment.channel_message_id)
        ).all()
    )
    total = session.scalar(
        select(func.count(ChannelMessageComment.id)).where(ChannelMessageComment.channel_target_id == channel_target_id)
    ) or 0
    return {
        "message_count_sample": len(message_rows),
        "comment_total": int(total),
        "per_message_sample": [
            {
                "channel_message_id": row.id,
                "message_id": row.message_id,
                "comment_available": bool(row.comment_available),
                "reference_count": int(counts.get(row.id) or 0),
            }
            for row in message_rows[:12]
        ],
    }


def action_summary(actions: list[Action]) -> dict:
    status_counts = Counter(action.status for action in actions)
    reply_count = sum(1 for action in actions if (action.payload or {}).get("reply_to_message_id"))
    mode_counts = Counter(str((action.payload or {}).get("comment_mode") or "") for action in actions)
    by_message: dict[int, Counter] = defaultdict(Counter)
    for action in actions:
        payload = action.payload if isinstance(action.payload, dict) else {}
        channel_message_id = int(payload.get("channel_message_id") or 0)
        if channel_message_id:
            by_message[channel_message_id]["total"] += 1
            if payload.get("reply_to_message_id"):
                by_message[channel_message_id]["reply"] += 1
    return {
        "total": len(actions),
        "reply": reply_count,
        "direct": len(actions) - reply_count,
        "status_counts": dict(status_counts),
        "comment_mode_counts": dict(mode_counts),
        "by_message": {str(key): dict(value) for key, value in sorted(by_message.items())[:20]},
    }


def main() -> None:
    with SessionLocal() as session:
        tasks = list(
            session.scalars(
                select(Task)
                .where(Task.type == "channel_comment", Task.deleted_at.is_(None))
                .order_by(Task.updated_at.desc())
                .limit(10)
            )
        )
        output = []
        for task in tasks:
            config = task.type_config if isinstance(task.type_config, dict) else {}
            stats = task.stats if isinstance(task.stats, dict) else {}
            channel_target_id = int(config.get("target_channel_id") or 0)
            channel = session.get(OperationTarget, channel_target_id) if channel_target_id else None
            recent_actions = list(
                session.scalars(
                    select(Action)
                    .where(
                        Action.task_id == task.id,
                        Action.action_type == "post_comment",
                        Action.created_at >= SINCE,
                    )
                    .order_by(Action.created_at.desc())
                    .limit(300)
                )
            )
            all_recent = list(
                session.scalars(
                    select(Action)
                    .where(Action.task_id == task.id, Action.action_type == "post_comment")
                    .order_by(Action.created_at.desc())
                    .limit(300)
                )
            )
            output.append(
                {
                    "task_id": task.id,
                    "name": task.name,
                    "status": task.status,
                    "last_error": task.last_error,
                    "next_run_at": iso(task.next_run_at),
                    "updated_at": iso(task.updated_at),
                    "target_channel_id": channel_target_id,
                    "target_channel_title": channel.title if channel else "",
                    "config": {
                        "target_comments_per_message": config.get("target_comments_per_message"),
                        "reply_min_per_message": config.get("reply_min_per_message"),
                        "comment_mode": config.get("comment_mode"),
                        "reply_to_message_ids": config.get("reply_to_message_ids"),
                        "message_scope": config.get("message_scope"),
                        "message_count": config.get("message_count"),
                        "message_ids": config.get("message_ids"),
                    },
                    "stats": {
                        "reply_planned_count": stats.get("reply_planned_count"),
                        "reply_target_shortfall_count": stats.get("reply_target_shortfall_count"),
                        "reply_candidate_shortfall_count": stats.get("reply_candidate_shortfall_count"),
                        "reply_success_count": stats.get("reply_success_count"),
                        "reply_failure_count": stats.get("reply_failure_count"),
                        "reply_payload_error_count": stats.get("reply_payload_error_count"),
                        "telegram_reply_failure_count": stats.get("telegram_reply_failure_count"),
                        "last_reply_failure_type": stats.get("last_reply_failure_type"),
                    },
                    "recent_24h_actions": action_summary(recent_actions),
                    "latest_actions": action_summary(all_recent),
                    "latest_samples": [short_action(action) for action in all_recent[:20]],
                    "comment_reference_pool": channel_comment_stats(session, channel_target_id) if channel_target_id else {},
                }
            )
        print("COMMENT_REPLY_INVESTIGATION", json.dumps(output, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()

from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from sqlalchemy import select, func
from app.database import SessionLocal
from app.models import Task, TaskDayLedger, ChannelMessage, Action, ViewRemoteFact, OperationTarget
from app.services._common import _now

BEIJING = ZoneInfo("Asia/Shanghai")

def inspect_channel_views():
    now = datetime.now(BEIJING)
    today = now.date()
    cutoff_7d = now.replace(tzinfo=None) - timedelta(days=7)

    with SessionLocal() as session:
        tasks = list(
            session.scalars(
                select(Task).where(
                    Task.status == "running",
                    Task.type == "channel_view",
                    Task.deleted_at.is_(None),
                )
            )
        )

        output = []
        for t in tasks:
            cfg = dict(t.type_config or {})
            pacing = dict(t.pacing_config or {})
            channel_id = int(cfg.get("target_channel_id") or 0)
            channel = session.get(OperationTarget, channel_id) if channel_id else None

            ledger = session.scalar(
                select(TaskDayLedger).where(
                    TaskDayLedger.task_id == t.id,
                    TaskDayLedger.obligation_local_date == today,
                )
            )

            all_msgs = list(session.scalars(
                select(ChannelMessage).where(
                    ChannelMessage.channel_target_id == channel_id,
                ).order_by(ChannelMessage.published_at.desc())
            ))
            msgs_within_7d = [m for m in all_msgs if m.published_at and m.published_at >= cutoff_7d]

            today_actions = list(session.scalars(
                select(Action).where(
                    Action.task_id == t.id,
                    Action.task_type == "channel_view",
                    Action.scheduled_at >= datetime(today.year, today.month, today.day),
                )
            ))

            output.append({
                "task_id": t.id,
                "task_name": t.name,
                "type_config": cfg,
                "curve": pacing.get("operation_profile", {}).get("hourly_activity_curve"),
                "channel_id": channel_id,
                "channel_title": channel.title if channel else None,
                "ledger_exists": ledger is not None,
                "ledger_status": ledger.lifecycle_status if ledger else None,
                "total_messages_in_db": len(all_msgs),
                "messages_within_7d": len(msgs_within_7d),
                "messages_within_7d_details": [
                    {"id": m.id, "message_id": m.message_id, "published_at": str(m.published_at)}
                    for m in msgs_within_7d[:5]
                ],
                "today_actions_count": len(today_actions),
                "today_actions_by_status": {
                    s: sum(1 for a in today_actions if a.status == s)
                    for s in set(a.status for a in today_actions)
                },
            })

        print("INSPECT_CHANNEL_VIEWS_RESULT=" + json.dumps(output, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    inspect_channel_views()

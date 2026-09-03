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
    today_start = datetime(today.year, today.month, today.day)
    cutoff_7d = now.replace(tzinfo=None) - timedelta(days=7)

    with SessionLocal() as session:
        tasks = list(
            session.scalars(
                select(Task).where(
                    Task.type == "channel_view",
                    Task.deleted_at.is_(None),
                ).order_by(Task.created_at.desc())
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
                    Action.scheduled_at >= today_start,
                )
            ))

            success_today = [a for a in today_actions if a.status == "success"]
            pending_today = [a for a in today_actions if a.status == "pending"]

            hourly_scheduled = {h: 0 for h in range(24)}
            hourly_executed = {h: 0 for h in range(24)}
            for a in today_actions:
                if a.scheduled_at:
                    h = a.scheduled_at.hour
                    hourly_scheduled[h] = hourly_scheduled.get(h, 0) + 1
                if a.executed_at and a.status == "success":
                    h = a.executed_at.hour
                    hourly_executed[h] = hourly_executed.get(h, 0) + 1

            facts_today = 0
            facts_total = 0
            if channel and channel.tg_peer_id:
                facts_today = session.scalar(
                    select(func.count(ViewRemoteFact.id)).where(
                        ViewRemoteFact.tenant_id == t.tenant_id,
                        ViewRemoteFact.target_peer_id == channel.tg_peer_id,
                        ViewRemoteFact.remote_confirmed_at >= today_start,
                    )
                ) or 0
                facts_total = session.scalar(
                    select(func.count(ViewRemoteFact.id)).where(
                        ViewRemoteFact.tenant_id == t.tenant_id,
                        ViewRemoteFact.target_peer_id == channel.tg_peer_id,
                    )
                ) or 0

            unique_accounts_today = len(set(a.account_id for a in today_actions if a.account_id))
            unique_success_accounts_today = len(set(a.account_id for a in success_today if a.account_id))

            output.append({
                "task_id": t.id,
                "task_name": t.name,
                "status": t.status,
                "last_error": t.last_error or "",
                "next_run_at": str(t.next_run_at) if t.next_run_at else None,
                "channel_id": channel_id,
                "channel_title": channel.title if channel else None,
                "channel_peer_id": channel.tg_peer_id if channel else None,
                "message_active_days": cfg.get("message_active_days"),
                "total_messages_in_db": len(all_msgs),
                "messages_within_7d": len(msgs_within_7d),
                "latest_message_time": str(all_msgs[0].published_at) if all_msgs else None,
                "messages_within_7d_ids": [m.message_id for m in msgs_within_7d],
                "ledger_exists": ledger is not None,
                "ledger_status": ledger.lifecycle_status if ledger else None,
                "today_actions_total": len(today_actions),
                "today_actions_pending": len(pending_today),
                "today_actions_success": len(success_today),
                "today_actions_failed": len([a for a in today_actions if a.status == "failed"]),
                "today_actions_by_status": {
                    s: sum(1 for a in today_actions if a.status == s)
                    for s in set(a.status for a in today_actions)
                },
                "unique_accounts_today": unique_accounts_today,
                "unique_success_accounts_today": unique_success_accounts_today,
                "view_facts_today": facts_today,
                "view_facts_total": facts_total,
                "hourly_scheduled": {h: c for h, c in hourly_scheduled.items() if c > 0},
                "hourly_executed": {h: c for h, c in hourly_executed.items() if c > 0},
                "pacing_curve_is_24h_uniform": pacing.get("operation_profile", {}).get("hourly_activity_curve") == [1] * 24,
            })

        print("INSPECT_CHANNEL_VIEWS_RESULT=" + json.dumps(output, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    inspect_channel_views()

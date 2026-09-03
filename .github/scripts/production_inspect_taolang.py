from __future__ import annotations
import json
from datetime import datetime
from zoneinfo import ZoneInfo
from sqlalchemy import select
from app.database import SessionLocal
from app.models import Task, TaskDayLedger, ChannelMessage, ChannelViewDailyMessageTarget, ViewRemoteFact, OperationTarget
from app.services.task_center.executors.channel_view import effective_channel_view_config, _view_target_scope, _view_plan_inputs, channel_scope

BEIJING = ZoneInfo("Asia/Shanghai")

def check_taolang():
    now = datetime.now(BEIJING)
    today = now.date()

    with SessionLocal() as session:
        task = session.scalar(
            select(Task).where(
                Task.name == "太郎日记",
                Task.type == "channel_view",
                Task.deleted_at.is_(None),
            )
        )
        if not task:
            print("Taolang task not found")
            return

        config = effective_channel_view_config(task)
        channel = session.get(OperationTarget, int(config.get("target_channel_id") or 0))

        ledger = session.scalar(
            select(TaskDayLedger).where(
                TaskDayLedger.task_id == task.id,
                TaskDayLedger.obligation_local_date == today,
            )
        )

        targets = list(session.scalars(
            select(ChannelViewDailyMessageTarget).where(
                ChannelViewDailyMessageTarget.task_day_ledger_id == (ledger.id if ledger else "")
            )
        ))

        target_info = []
        for tg in targets:
            # check facts for this message
            facts_count = session.scalar(
                select(ViewRemoteFact).where(
                    ViewRemoteFact.channel_message_id == tg.channel_message_id
                )
            )
            target_info.append({
                "message_id": tg.channel_message_id,
                "daily_target_snapshot": tg.daily_target_snapshot,
                "effective_target_snapshot": tg.effective_target_snapshot,
                "total_target_snapshot": tg.total_target_snapshot,
                "lifetime_confirmed_at_attach": tg.lifetime_confirmed_at_attach,
                "due_count": tg.due_count,
                "source_state": tg.source_state,
                "active_until": str(tg.active_until),
            })

        # Test channel_scope
        scoped_channel, selected_msgs = channel_scope(session, task, config)
        
        # Test scope
        scope = _view_target_scope(session, task, channel, selected_messages=selected_msgs, config=config)
        
        prepared = _view_plan_inputs(session, task, scope, config=config)
        prepared_result = None
        if prepared is not None:
            inputs, completed_counts, total_target = prepared
            prepared_result = {
                "inputs_accounts_count": len(inputs.accounts),
                "task_remaining_today": inputs.task_remaining_today,
                "messages_count": len(inputs.messages),
                "completed_counts": completed_counts,
                "total_target": total_target,
            }

        print("TAOLANG_DIAGNOSTICS=" + json.dumps({
            "task_id": task.id,
            "config": config,
            "ledger_id": ledger.id if ledger else None,
            "targets_count": len(targets),
            "target_info": target_info,
            "selected_messages": [m.id for m in selected_msgs],
            "prepared_is_none": prepared is None,
            "prepared_result": prepared_result,
            "task_last_error": task.last_error,
        }, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    check_taolang()

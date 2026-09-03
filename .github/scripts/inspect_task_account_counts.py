from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from sqlalchemy import func, select
from app.database import SessionLocal
from app.models import (
    AccountPool,
    Action,
    ExecutionAttempt,
    Task,
    TgAccount,
    TgGroupAccount,
)
from app.services.task_center.account_pool import select_task_accounts


def inspect_all_task_accounts() -> list[dict[str, Any]]:
    results = []
    now = datetime.now(tz=UTC)
    since_24h = now - timedelta(hours=24)

    with SessionLocal() as session:
        tasks = list(
            session.scalars(
                select(Task)
                .where(
                    Task.deleted_at.is_(None),
                    Task.status.in_(("running", "paused")),
                )
                .order_by(Task.type, Task.status, Task.name)
            )
        )

        for task in tasks:
            config = dict(task.account_config or {})
            type_cfg = dict(task.type_config or {})
            selection_mode = config.get("selection_mode") or "all"
            max_concurrent = config.get("max_concurrent")

            # 1. Available candidate accounts via service
            try:
                candidate_accounts = select_task_accounts(
                    session,
                    task.tenant_id,
                    config,
                    enforce_max_concurrent=False,
                    enforce_capacity=False,
                )
                candidate_count = len(candidate_accounts)
            except Exception as e:
                candidate_count = -1

            # 2. If group_ai_chat, how many accounts in group can_send
            target_group_id = type_cfg.get("target_group_id")
            group_send_count = None
            if task.type == "group_ai_chat" and target_group_id:
                try:
                    group_send_count = session.scalar(
                        select(func.count(TgGroupAccount.id))
                        .join(TgAccount, TgAccount.id == TgGroupAccount.account_id)
                        .where(
                            TgGroupAccount.group_id == int(target_group_id),
                            TgGroupAccount.can_send.is_(True),
                            TgAccount.tenant_id == task.tenant_id,
                            TgAccount.status == "active",
                            TgAccount.deleted_at.is_(None),
                        )
                    ) or 0
                except Exception:
                    group_send_count = None

            # 3. Distinct accounts with successful attempts in last 24h
            active_24h_accounts = session.scalar(
                select(func.count(func.distinct(Action.account_id)))
                .join(ExecutionAttempt, ExecutionAttempt.action_id == Action.id)
                .where(
                    Action.task_id == task.id,
                    ExecutionAttempt.status == "success",
                    ExecutionAttempt.remote_message_id.is_not(None),
                    ExecutionAttempt.remote_message_id != "",
                    ExecutionAttempt.after_call_at >= since_24h,
                )
            ) or 0

            # 4. Total successful messages in last 24h
            total_24h_messages = session.scalar(
                select(func.count(ExecutionAttempt.id))
                .join(Action, Action.id == ExecutionAttempt.action_id)
                .where(
                    Action.task_id == task.id,
                    ExecutionAttempt.status == "success",
                    ExecutionAttempt.remote_message_id.is_not(None),
                    ExecutionAttempt.remote_message_id != "",
                    ExecutionAttempt.after_call_at >= since_24h,
                )
            ) or 0

            # 5. Distinct accounts in open/pending actions right now
            open_action_accounts = session.scalar(
                select(func.count(func.distinct(Action.account_id)))
                .where(
                    Action.task_id == task.id,
                    Action.status.in_(("pending", "executing")),
                )
            ) or 0

            results.append({
                "task_id": task.id,
                "task_name": task.name,
                "task_type": task.type,
                "status": task.status,
                "account_selection_mode": selection_mode,
                "max_concurrent": max_concurrent,
                "candidate_pool_accounts": candidate_count,
                "group_can_send_accounts": group_send_count,
                "active_24h_distinct_accounts": active_24h_accounts,
                "total_24h_success_messages": total_24h_messages,
                "open_action_accounts": open_action_accounts,
            })

    return results


def main() -> None:
    data = inspect_all_task_accounts()
    print("TASK_ACCOUNT_INSPECTION_RESULT=" + json.dumps(data, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()

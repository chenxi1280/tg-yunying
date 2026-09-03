#!/usr/bin/env python3
"""恢复并对齐频道评论任务配置与状态脚本。

支持：
1. 预览 (preview) 与 执行 (apply) 双模式；
2. 全量账号关注模式 (selection_mode: 'all')，解除 20 个账号上限瓶颈；
3. 3~5 日多日履约窗口 (rolling_window_days: 3~5)；
4. 日风控翻倍与阶梯爬坡 (multi_day_rampup: True)；
5. 新旧账号混合互动模型 (allow_returning_accounts: True)；
6. 自动跳过已知废弃重复任务（如旧版成都阿楠 e6e295d8）；
7. 打印标准化 JSON 摘要供 GitHub Actions 或调度器回读。
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import sys
from typing import Any

from sqlalchemy import select
from app.database import SessionLocal
from app.models import Task
from app.services.task_center.service import (
    _apply_type_config_data,
    resume_task,
)

DEFAULT_TARGET_COMMENTS = 100
DEFAULT_REPLY_MIN = 20
DEFAULT_COMMENT_MODE = "mixed"
DEFAULT_MESSAGE_SCOPE = "dynamic_new"
DEFAULT_ROLLING_WINDOW_DAYS = 3
DEFAULT_MULTI_DAY_RAMPUP = True
DEFAULT_ALLOW_RETURNING = True
DEFAULT_ALL_ACCOUNTS = True
DEFAULT_ACTOR = "prod-upgrade-comment-tasks-multi-day"

# Known duplicate legacy tasks that should not be resumed to avoid multi-task conflict on the same channel
RETIRED_TASK_IDS = {
    "e6e295d8-746f-4015-9be9-8e71ecbbfd54",  # 成都阿楠旧版 (superseded by ef94507d)
}


def build_desired_config(
    task: Task,
    *,
    target_comments: int,
    reply_min: int,
    comment_mode: str,
    message_scope: str,
    rolling_window_days: int,
    allow_returning_accounts: bool,
) -> dict[str, Any]:
    existing = dict(task.type_config or {})
    changes: dict[str, Any] = {}
    if int(existing.get("target_comments_per_message") or 0) != target_comments:
        changes["target_comments_per_message"] = target_comments
    if int(existing.get("reply_min_per_message") or 0) != reply_min:
        changes["reply_min_per_message"] = reply_min
    if existing.get("comment_mode") != comment_mode:
        changes["comment_mode"] = comment_mode
    if existing.get("message_scope") != message_scope:
        changes["message_scope"] = message_scope
    if int(existing.get("rolling_window_days") or 0) != rolling_window_days:
        changes["rolling_window_days"] = rolling_window_days
    if bool(existing.get("allow_returning_accounts")) != allow_returning_accounts:
        changes["allow_returning_accounts"] = allow_returning_accounts
    return changes


def build_desired_pacing(
    task: Task,
    *,
    rolling_window_days: int,
    multi_day_rampup: bool,
) -> dict[str, Any]:
    existing = dict(task.pacing_config or {})
    changes: dict[str, Any] = {}
    if int(existing.get("rolling_window_days") or 0) != rolling_window_days:
        changes["rolling_window_days"] = rolling_window_days
    if bool(existing.get("multi_day_rampup")) != multi_day_rampup:
        changes["multi_day_rampup"] = multi_day_rampup
    return changes


def build_desired_account_config(
    task: Task,
    *,
    all_accounts: bool,
) -> dict[str, Any]:
    if not all_accounts:
        return {}
    existing = dict(task.account_config or {})
    if existing.get("selection_mode") == "all" and not existing.get("account_ids"):
        return {}
    return {"selection_mode": "all"}


def snapshot_task(task: Task) -> dict[str, Any]:
    config = dict(task.type_config or {})
    pacing = dict(task.pacing_config or {})
    account = dict(task.account_config or {})
    return {
        "task_id": task.id,
        "task_name": task.name,
        "task_type": task.type,
        "status": task.status,
        "tenant_id": task.tenant_id,
        "target_channel_id": config.get("target_channel_id"),
        "target_comments_per_message": config.get("target_comments_per_message"),
        "reply_min_per_message": config.get("reply_min_per_message"),
        "comment_mode": config.get("comment_mode"),
        "message_scope": config.get("message_scope"),
        "rolling_window_days": config.get("rolling_window_days") or pacing.get("rolling_window_days"),
        "multi_day_rampup": pacing.get("multi_day_rampup"),
        "allow_returning_accounts": config.get("allow_returning_accounts"),
        "account_selection_mode": account.get("selection_mode"),
        "config_revision": task.config_revision,
        "lifecycle_epoch": task.task_lifecycle_epoch,
        "next_run_at": task.next_run_at.isoformat() if task.next_run_at else None,
        "last_error": task.last_error or "",
    }


def plan_task_action(
    task: Task,
    *,
    target_comments: int,
    reply_min: int,
    comment_mode: str,
    message_scope: str,
    rolling_window_days: int,
    multi_day_rampup: bool,
    allow_returning_accounts: bool,
    all_accounts: bool,
) -> dict[str, Any]:
    is_retired = task.id in RETIRED_TASK_IDS
    should_resume = (task.status == "paused") and not is_retired
    config_changes = build_desired_config(
        task,
        target_comments=target_comments,
        reply_min=reply_min,
        comment_mode=comment_mode,
        message_scope=message_scope,
        rolling_window_days=rolling_window_days,
        allow_returning_accounts=allow_returning_accounts,
    )
    pacing_changes = build_desired_pacing(
        task,
        rolling_window_days=rolling_window_days,
        multi_day_rampup=multi_day_rampup,
    )
    account_changes = build_desired_account_config(
        task,
        all_accounts=all_accounts,
    )
    return {
        "task_id": task.id,
        "task_name": task.name,
        "current_status": task.status,
        "is_retired": is_retired,
        "should_resume": should_resume,
        "config_changes": config_changes,
        "pacing_changes": pacing_changes,
        "account_changes": account_changes,
        "target_status": "running" if (task.status == "running" or should_resume) else task.status,
    }


def execute_recovery(
    *,
    mode: str,
    target_comments: int = DEFAULT_TARGET_COMMENTS,
    reply_min: int = DEFAULT_REPLY_MIN,
    comment_mode: str = DEFAULT_COMMENT_MODE,
    message_scope: str = DEFAULT_MESSAGE_SCOPE,
    rolling_window_days: int = DEFAULT_ROLLING_WINDOW_DAYS,
    multi_day_rampup: bool = DEFAULT_MULTI_DAY_RAMPUP,
    allow_returning_accounts: bool = DEFAULT_ALLOW_RETURNING,
    all_accounts: bool = DEFAULT_ALL_ACCOUNTS,
    actor: str = DEFAULT_ACTOR,
) -> dict[str, Any]:
    apply = (mode == "apply")
    with SessionLocal() as session:
        tasks = list(session.scalars(
            select(Task)
            .where(
                Task.type == "channel_comment",
                Task.deleted_at.is_(None),
            )
            .order_by(Task.name, Task.id)
        ))

        before_snapshots = [snapshot_task(t) for t in tasks]
        actions_planned = [
            plan_task_action(
                t,
                target_comments=target_comments,
                reply_min=reply_min,
                comment_mode=comment_mode,
                message_scope=message_scope,
                rolling_window_days=rolling_window_days,
                multi_day_rampup=multi_day_rampup,
                allow_returning_accounts=allow_returning_accounts,
                all_accounts=all_accounts,
            )
            for t in tasks
        ]

        execution_logs = []

        if apply:
            for plan in actions_planned:
                task_id = plan["task_id"]
                task = session.get(Task, task_id)
                if not task:
                    continue

                # 1. Update type_config if needed
                if plan["config_changes"]:
                    _apply_type_config_data(
                        session,
                        tenant_id=task.tenant_id,
                        task_id=task.id,
                        expected_type="channel_comment",
                        update_data=plan["config_changes"],
                        actor=actor,
                    )
                    execution_logs.append({
                        "task_id": task_id,
                        "action": "update_config",
                        "changes": plan["config_changes"],
                    })

                # 2. Update pacing_config if needed
                if plan["pacing_changes"]:
                    new_pacing = dict(task.pacing_config or {})
                    new_pacing.update(plan["pacing_changes"])
                    task.pacing_config = new_pacing
                    execution_logs.append({
                        "task_id": task_id,
                        "action": "update_pacing_config",
                        "changes": plan["pacing_changes"],
                    })

                # 3. Update account_config if needed (switch to all-account mode)
                if plan["account_changes"]:
                    task.account_config = plan["account_changes"]
                    execution_logs.append({
                        "task_id": task_id,
                        "action": "update_account_config",
                        "changes": plan["account_changes"],
                    })

                # 4. Resume if paused and not retired
                if plan["should_resume"]:
                    resume_task(
                        session,
                        tenant_id=task.tenant_id,
                        task_id=task.id,
                        actor=actor,
                    )
                    execution_logs.append({
                        "task_id": task_id,
                        "action": "resume_task",
                        "from_status": "paused",
                        "to_status": "running",
                    })

            session.commit()
            session.expire_all()

        # Read back current state
        refreshed_tasks = list(session.scalars(
            select(Task)
            .where(
                Task.type == "channel_comment",
                Task.deleted_at.is_(None),
            )
            .order_by(Task.name, Task.id)
        ))
        after_snapshots = [snapshot_task(t) for t in refreshed_tasks]

        summary = {
            "mode": mode,
            "executed_at": datetime.now(tz=UTC).isoformat(),
            "total_comment_tasks": len(tasks),
            "resumed_task_count": sum(1 for p in actions_planned if p["should_resume"]),
            "config_updated_task_count": sum(1 for p in actions_planned if p["config_changes"] or p["pacing_changes"] or p["account_changes"]),
            "running_task_count": sum(1 for s in after_snapshots if s["status"] == "running"),
            "paused_task_count": sum(1 for s in after_snapshots if s["status"] == "paused"),
        }

        return {
            "summary": summary,
            "actions_planned": actions_planned,
            "execution_logs": execution_logs,
            "before_snapshots": before_snapshots,
            "after_snapshots": after_snapshots,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="恢复并升级频道评论任务至全员关注与 3~5 日多日阶梯模式")
    parser.add_argument("--mode", choices=["preview", "apply"], default="preview", help="运行模式 (preview 为只读预览, apply 为执行变更)")
    parser.add_argument("--target-comments", type=int, default=DEFAULT_TARGET_COMMENTS, help="单帖目标评论数 (默认 100)")
    parser.add_argument("--reply-min", type=int, default=DEFAULT_REPLY_MIN, help="单帖保底引用回复数 (默认 20)")
    parser.add_argument("--comment-mode", choices=["comment", "reply", "mixed"], default=DEFAULT_COMMENT_MODE, help="评论模式 (默认 mixed)")
    parser.add_argument("--message-scope", choices=["all", "latest_n", "dynamic_new"], default=DEFAULT_MESSAGE_SCOPE, help="消息监听范围 (默认 dynamic_new)")
    parser.add_argument("--rolling-window-days", type=int, default=DEFAULT_ROLLING_WINDOW_DAYS, help="履约窗口天数 (默认 3 天)")
    parser.add_argument("--multi-day-rampup", action="store_true", default=DEFAULT_MULTI_DAY_RAMPUP, help="开启日风控翻倍阶梯爬坡 (默认开启)")
    parser.add_argument("--allow-returning-accounts", action="store_true", default=DEFAULT_ALLOW_RETURNING, help="开启次日新旧账号回访混合互动 (默认开启)")
    parser.add_argument("--all-accounts", action="store_true", default=DEFAULT_ALL_ACCOUNTS, help="开启全量账号关注模式 (默认开启)")
    parser.add_argument("--actor", default=DEFAULT_ACTOR, help="操作人标识")

    args = parser.parse_args()

    result = execute_recovery(
        mode=args.mode,
        target_comments=args.target_comments,
        reply_min=args.reply_min,
        comment_mode=args.comment_mode,
        message_scope=args.message_scope,
        rolling_window_days=args.rolling_window_days,
        multi_day_rampup=args.multi_day_rampup,
        allow_returning_accounts=args.allow_returning_accounts,
        all_accounts=args.all_accounts,
        actor=args.actor,
    )

    print("CHANNEL_COMMENT_RECOVERY_RESULT=" + json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()

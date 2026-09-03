from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Add backend root to sys.path
BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from sqlalchemy import select
from app.database import SessionLocal
from app.models import Task
from app.services.task_center.service import (
    _apply_type_config_data,
    resume_task,
)

DEFAULT_TARGET_COMMENTS = 20
DEFAULT_REPLY_MIN = 5
DEFAULT_COMMENT_MODE = "mixed"
DEFAULT_MESSAGE_SCOPE = "dynamic_new"
DEFAULT_ACTOR = "prod-resume-and-align-comment-tasks"

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
    return changes


def snapshot_task(task: Task) -> dict[str, Any]:
    config = dict(task.type_config or {})
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
) -> dict[str, Any]:
    is_retired = task.id in RETIRED_TASK_IDS
    should_resume = (task.status == "paused") and not is_retired
    config_changes = build_desired_config(
        task,
        target_comments=target_comments,
        reply_min=reply_min,
        comment_mode=comment_mode,
        message_scope=message_scope,
    )
    return {
        "task_id": task.id,
        "task_name": task.name,
        "current_status": task.status,
        "is_retired": is_retired,
        "should_resume": should_resume,
        "config_changes": config_changes,
        "target_status": "running" if (task.status == "running" or should_resume) else task.status,
    }


def execute_recovery(
    *,
    mode: str,
    target_comments: int = DEFAULT_TARGET_COMMENTS,
    reply_min: int = DEFAULT_REPLY_MIN,
    comment_mode: str = DEFAULT_COMMENT_MODE,
    message_scope: str = DEFAULT_MESSAGE_SCOPE,
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

                # 1. Update config if needed
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

                # 2. Resume if paused and not retired
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
            "config_updated_task_count": sum(1 for p in actions_planned if p["config_changes"]),
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
    parser = argparse.ArgumentParser(description="恢复并对齐频道评论任务配置与状态")
    parser.add_argument("--mode", choices=["preview", "apply"], default="preview", help="运行模式 (preview 为只读预览, apply 为执行变更)")
    parser.add_argument("--target-comments", type=int, default=DEFAULT_TARGET_COMMENTS, help="单帖目标评论数 (默认 20)")
    parser.add_argument("--reply-min", type=int, default=DEFAULT_REPLY_MIN, help="单帖保底引用回复数 (默认 5)")
    parser.add_argument("--comment-mode", choices=["comment", "reply", "mixed"], default=DEFAULT_COMMENT_MODE, help="评论模式 (默认 mixed)")
    parser.add_argument("--message-scope", choices=["all", "latest_n", "dynamic_new"], default=DEFAULT_MESSAGE_SCOPE, help="消息监听范围 (默认 dynamic_new)")
    parser.add_argument("--actor", default=DEFAULT_ACTOR, help="操作人标识")

    args = parser.parse_args()

    result = execute_recovery(
        mode=args.mode,
        target_comments=args.target_comments,
        reply_min=args.reply_min,
        comment_mode=args.comment_mode,
        message_scope=args.message_scope,
        actor=args.actor,
    )

    print("CHANNEL_COMMENT_RECOVERY_RESULT=" + json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()

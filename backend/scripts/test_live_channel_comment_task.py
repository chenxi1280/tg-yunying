from __future__ import annotations

import argparse
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

# Add backend root to sys.path
BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from sqlalchemy import select
from app.database import SessionLocal
from app.models import (
    Action,
    ChannelMessage,
    CommentFulfillmentObligation,
    ExecutionAttempt,
    OperationTarget,
    Task,
)
from app.services._common import _now
from app.services.task_center.source_pacing import rolling_source_window, wall_datetime

BEIJING = ZoneInfo("Asia/Shanghai")


def inspect_live_comment_task(task_id: str | None = None, task_name: str | None = None) -> None:
    with SessionLocal() as session:
        now_value = _now()
        task = _find_task(session, task_id=task_id, task_name=task_name)
        print(f"=== 频道评论任务诊断工具 (运行时间: {now_value:%Y-%m-%d %H:%M:%S}) ===")
        _print_task_config(session, task)
        _print_obligation_summary(session, task, now_value=now_value)
        _print_attempt_summary(session, task)
        channel_id = int((task.type_config or {}).get("target_channel_id") or 0)
        _print_message_windows(
            session, task, channel_id=channel_id, now_value=now_value,
        )
        print("\n=== 诊断完成 ===")


def _find_task(session, *, task_id: str | None, task_name: str | None) -> Task:
    query = select(Task).where(
        Task.type == "channel_comment",
        Task.deleted_at.is_(None),
    )
    if task_id:
        query = query.where(Task.id.startswith(task_id))
    elif task_name:
        query = query.where(Task.name.ilike(f"%{task_name}%"))
    task = session.scalars(query.order_by(Task.created_at.desc())).first()
    if task is None:
        raise LookupError("channel_comment_task_not_found")
    return task


def _print_task_config(session, task: Task) -> None:
    config = task.type_config or {}
    channel_id = int(config.get("target_channel_id") or 0)
    channel = session.get(OperationTarget, channel_id)
    print(f"\n[1] 目标任务基础信息: 【{task.name}】 ({task.id})")
    print(f"  - 状态: {task.status} | 履约合同版本: {task.fulfillment_contract_version}")
    print(f"  - 时区: {task.timezone} | 最近报错: {task.last_error or '无'}")
    print(f"  - 下次调度时间: {task.next_run_at}")
    print("\n[2] 任务配置明细:")
    print(f"  - 目标频道: {channel.title if channel else '未知'} (ID: {channel_id})")
    print(f"  - 消息范围 (message_scope): {config.get('message_scope')}")
    print(f"  - 评论模式 (comment_mode): {config.get('comment_mode')}")
    print(f"  - 单帖目标量 (target_comments_per_message): {config.get('target_comments_per_message')}")
    print(f"  - 数量抖动 (comment_count_jitter): {config.get('comment_count_jitter', '未配置')}")
    print(f"  - 滚动窗口天数 (rolling_window_days): {config.get('rolling_window_days', '1 (默认24小时)')}")
    print(f"  - 每日上限 (daily_comment_cap): {config.get('daily_comment_cap', '未限制')}")


def _print_obligation_summary(session, task: Task, *, now_value) -> None:
    obligations = session.scalars(select(CommentFulfillmentObligation).where(
        CommentFulfillmentObligation.task_id == task.id,
    )).all()
    status_counts: dict[str, int] = {}
    for obligation in obligations:
        status_counts[obligation.status] = status_counts.get(obligation.status, 0) + 1
    now_wall = wall_datetime(now_value)
    due_count = sum(
        1 for item in obligations
        if item.pacing_due_at and wall_datetime(item.pacing_due_at) <= now_wall
    )
    missing_due_count = sum(
        1 for item in obligations
        if item.pacing_due_at is None and item.status in ("open", "replan_required")
    )
    print("\n[3] 任务义务 (Obligations) 统计:")
    print(f"  - 义务总数: {len(obligations)}")
    print(f"  - 状态分布: {status_counts}")
    print(f"  - 当前已到期数 (due): {due_count}")
    print(f"  - 悬挂未排期数 (due_at_missing): {missing_due_count}")


def _print_attempt_summary(session, task: Task) -> None:
    actions = session.scalars(
        select(Action).where(Action.task_id == task.id)
        .order_by(Action.created_at.desc()).limit(20)
    ).all()
    statuses: dict[str, int] = {}
    for action in actions:
        statuses[action.status] = statuses.get(action.status, 0) + 1
    attempts = session.scalars(
        select(ExecutionAttempt).join(Action, Action.id == ExecutionAttempt.action_id)
        .where(
            Action.task_id == task.id,
            ExecutionAttempt.status == "success",
            ExecutionAttempt.remote_message_id.is_not(None),
            ExecutionAttempt.remote_message_id != "",
        ).order_by(ExecutionAttempt.created_at.desc()).limit(5)
    ).all()
    print(f"\n[4] 真实 Action 履约事实统计 (采样最近 {len(actions)} 条):")
    print(f"  - Action 状态分布: {statuses}")
    print(f"  - 远端发送成功证据 (最近 {len(attempts)} 条):")
    for attempt in attempts:
        print(
            f"    * Attempt ID: {attempt.id[:8]}... | "
            f"Remote Message ID: {attempt.remote_message_id} | 发送时间: {attempt.created_at}"
        )


def _print_message_windows(
    session,
    task: Task,
    *,
    channel_id: int,
    now_value,
) -> None:
    messages = session.scalars(
        select(ChannelMessage).where(
            ChannelMessage.tenant_id == task.tenant_id,
            ChannelMessage.channel_target_id == channel_id,
        ).order_by(ChannelMessage.created_at.desc()).limit(5)
    ).all()
    print(f"\n[5] 频道消息与滚动排期窗口状态 (最近 {len(messages)} 条):")
    for message in messages:
        window_start, window_end = rolling_source_window(task, message.created_at)
        expired = window_end <= wall_datetime(now_value)
        print(
            f"  - 消息 #{message.message_id} (ID: {message.id}) | "
            f"发布/采集时间: {message.created_at:%Y-%m-%d %H:%M}"
        )
        print(
            f"    * 窗口区间: [{window_start:%m-%d %H:%M} ~ "
            f"{window_end:%m-%d %H:%M}] | 是否已过期: {'已过期' if expired else '有效期内'}"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="频道评论任务诊断工具")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--task-id", help="任务 ID 或前缀")
    target.add_argument("--task-name", help="任务名称模糊匹配")
    args = parser.parse_args()
    inspect_live_comment_task(task_id=args.task_id, task_name=args.task_name)

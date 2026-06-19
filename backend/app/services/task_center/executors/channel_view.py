from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Action, ChannelMessage, OperationTarget, Task
from app.services._common import _now

from ..account_pool import daily_uncovered_account_count, select_task_accounts
from ..channel_membership import channel_member_accounts, gate_channel_membership
from ..pacing import schedule_times
from ..payloads import ViewMessagePayload, create_view_action
from .common import adjust_for_account_hour_limit, available_channel_accounts_for_message_date, channel_message_account_ids, channel_message_payload, channel_scope, quantity_jitter_bounds, quantity_with_jitter, record_channel_capacity_warning


def build_plan(session: Session, task: Task) -> int:
    config = task.type_config or {}
    channel = session.get(OperationTarget, int(config.get("target_channel_id") or 0))
    if not channel or channel.tenant_id != task.tenant_id or channel.target_type != "channel":
        task.last_error = "目标频道不存在"
        return 0
    gate = gate_channel_membership(session, task, channel)
    if not gate.ready:
        return gate.created
    channel, messages = channel_scope(session, task, config)
    if not channel or not messages:
        return 0
    daily_target = int(config.get("per_message_daily_view_target") or config.get("target_views_per_message") or 1)
    total_target = max(daily_target, int(config.get("per_message_total_view_target") or config.get("target_views_per_message") or daily_target))
    task_daily_cap = int(config.get("task_daily_view_safety_cap") or 0)
    effective_daily_cap = task_daily_cap if task_daily_cap > 0 else None
    accounts = _view_accounts(session, task, channel, config)
    if not accounts:
        task.last_error = "没有可用账号，等待账号恢复后继续执行"
        return 0
    record_channel_capacity_warning(task, "浏览", daily_target, len(accounts))
    execution_date = _now().date().isoformat()
    task_remaining_today = _remaining_task_daily_capacity(session, task, execution_date, effective_daily_cap)
    if task_remaining_today <= 0:
        task.last_error = "任务今日浏览安全上限已用完，等待下一日继续规划"
        return 0
    actions = _view_actions_for_messages(
        session,
        task,
        config,
        ViewPlanInputs(
            messages=messages,
            accounts=accounts,
            execution_date=execution_date,
            daily_target=daily_target,
            total_target=total_target,
            task_remaining_today=task_remaining_today,
        ),
    )
    if not actions:
        task.last_error = task.last_error or "没有可新增的有效浏览账号"
        return 0
    return _create_view_actions(session, task, channel, config, actions, execution_date, daily_target, total_target)


def _view_accounts(session: Session, task: Task, channel: OperationTarget, config: dict) -> list:
    daily_target = int(config.get("per_message_daily_view_target") or config.get("target_views_per_message") or 1)
    task_daily_cap = int(config.get("task_daily_view_safety_cap") or 0)
    _lower, max_target_per_message = quantity_jitter_bounds(daily_target, float(config.get("view_count_jitter") or 0))
    account_scan_limit = max(
        max_target_per_message,
        task_daily_cap if task_daily_cap > 0 else 0,
        int((task.account_config or {}).get("max_concurrent") or max_target_per_message),
    )
    return channel_member_accounts(
        session,
        task,
        channel,
        select_task_accounts(
            session,
            task.tenant_id,
            task.account_config or {},
            limit=account_scan_limit,
            enforce_max_concurrent=False,
            daily_coverage_task_id=task.id,
            daily_coverage_action_types=("view_message",),
        ),
    )


def _create_view_actions(
    session: Session,
    task: Task,
    channel: OperationTarget,
    config: dict,
    actions: list[tuple[ChannelMessage, int]],
    execution_date: str,
    daily_target: int,
    total_target: int,
) -> int:
    times = schedule_times(len(actions), task.pacing_config or {})
    created = 0
    for index, (message, account_id) in enumerate(actions):
        planned_at = adjust_for_account_hour_limit(session, task, account_id, "view_message", times[index], config)
        payload = {
            **channel_message_payload(channel, message),
            "execution_date": execution_date,
            "daily_view_target": daily_target,
            "total_view_target": total_target,
        }
        create_view_action(session, task, account_id, planned_at, ViewMessagePayload(**payload))
        created += 1
    return created


def _view_actions_for_messages(
    session: Session,
    task: Task,
    config: dict,
    inputs: "ViewPlanInputs",
) -> list[tuple[ChannelMessage, int]]:
    coverage_remaining = daily_uncovered_account_count(session, task.id, ("view_message",), inputs.accounts)
    actions: list[tuple[ChannelMessage, int]] = []
    task_remaining_today = inputs.task_remaining_today
    for message in inputs.messages:
        if _message_expired(message, config):
            continue
        quantity = _view_quantity_for_message(session, task, config, inputs, message, coverage_remaining)
        quantity = min(quantity, task_remaining_today)
        if quantity <= 0:
            continue
        candidates = available_channel_accounts_for_message_date(session, task, "view_message", message, inputs.accounts, inputs.execution_date)
        selected = [account for account in candidates if _account_has_view_daily_capacity(session, task, account.id, inputs.execution_date, config)][:quantity]
        actions.extend((message, account.id) for account in selected)
        coverage_remaining = max(0, coverage_remaining - len(selected))
        task_remaining_today -= len(selected)
        if task_remaining_today <= 0:
            break
    return actions


@dataclass(frozen=True)
class ViewPlanInputs:
    messages: list[ChannelMessage]
    accounts: list
    execution_date: str
    daily_target: int
    total_target: int
    task_remaining_today: int


def _view_quantity_for_message(
    session: Session,
    task: Task,
    config: dict,
    inputs: ViewPlanInputs,
    message: ChannelMessage,
    coverage_remaining: int,
) -> int:
    base = quantity_with_jitter(inputs.daily_target, float(config.get("view_count_jitter") or 0))
    completed_count = _completed_view_count(session, task, message)
    if completed_count >= inputs.total_target:
        return 0
    used_count = len(channel_message_account_ids(session, task, "view_message", message, execution_date=inputs.execution_date))
    return max(0, min(max(base, coverage_remaining), inputs.total_target - completed_count) - used_count)


def _remaining_task_daily_capacity(session: Session, task: Task, execution_date: str, daily_cap: int | None) -> int:
    if daily_cap is None:
        return 100000000
    planned_today = 0
    for payload in session.scalars(
        select(Action.payload).where(
            Action.task_id == task.id,
            Action.action_type == "view_message",
            Action.status.in_(["pending", "executing", "success"]),
        )
    ):
        if isinstance(payload, dict) and str(payload.get("execution_date") or "") == execution_date:
            planned_today += 1
    return max(0, daily_cap - planned_today)


def _completed_view_count(session: Session, task: Task, message: ChannelMessage) -> int:
    return int(
        session.scalar(
            select(func.count(Action.id)).where(
                Action.task_id == task.id,
                Action.action_type == "view_message",
                Action.status == "success",
                Action.payload["channel_message_id"].as_integer() == message.id,
            )
        )
        or 0
    )


def _account_has_view_daily_capacity(session: Session, task: Task, account_id: int, execution_date: str, config: dict) -> bool:
    limit = int(config.get("max_views_per_account_per_day") or 0)
    if limit <= 0:
        return True
    planned = 0
    for payload in session.scalars(
        select(Action.payload).where(
            Action.task_id == task.id,
            Action.account_id == account_id,
            Action.action_type == "view_message",
            Action.status.in_(["pending", "executing", "success"]),
        )
    ):
        if isinstance(payload, dict) and str(payload.get("execution_date") or "") == execution_date:
            planned += 1
    return planned < limit


def _message_expired(message: ChannelMessage, config: dict) -> bool:
    active_days = int(config.get("message_active_days") or 0)
    if active_days <= 0 or not message.published_at:
        return False
    return message.published_at < _now() - timedelta(days=active_days)


__all__ = ["build_plan"]

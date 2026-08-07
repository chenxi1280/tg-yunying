from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.models import ChannelMessage, OperationTarget, Task, TaskDayLedger
from app.services._common import _now

from ..account_pool import daily_uncovered_account_count, select_task_accounts
from ..channel_fulfillment import (
    bind_obligation_action,
    ensure_view_obligation,
    obligation_accepts_new_action,
    view_account_ids_for_messages,
    view_confirmed_counts,
    view_daily_counts,
)
from ..channel_membership import channel_member_accounts, gate_channel_membership
from ..daily_ledgers import ensure_task_day_ledger
from ..fulfillment_activation import CURRENT_CONTRACT_VERSION
from ..pacing import (
    cumulative_pacing_due,
    next_local_day_deadline,
    schedule_times,
    task_pacing_anchor,
)
from ..payloads import ViewMessagePayload, create_view_action
from .common import adjust_for_account_hour_limit, channel_message_payload, channel_scope, quantity_jitter_bounds, quantity_with_jitter, record_channel_capacity_warning


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
    now_value = _now()
    ledger = ensure_task_day_ledger(session, task, now=now_value)
    execution_date = ledger.obligation_local_date.isoformat()
    daily_counts = view_daily_counts(session, ledger)
    task_remaining_today = _remaining_task_daily_capacity(effective_daily_cap, daily_counts.total)
    if task_remaining_today <= 0:
        task.last_error = "任务今日浏览安全上限已用完，等待下一日继续规划"
        return 0
    account_ids_by_message = view_account_ids_for_messages(
        session,
        task,
        ledger,
        messages,
    )
    completed_counts = view_confirmed_counts(session, task, messages)
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
            completed_counts=completed_counts,
            daily_counts_by_account=daily_counts.by_account,
            ledger=ledger,
            now=now_value,
        ),
        account_ids_by_message,
    )
    if not actions:
        task.last_error = _empty_view_plan_message(task, config, messages, total_target, completed_counts)
        return 0
    context = ViewCreationContext(
        channel=channel,
        config=config,
        execution_date=execution_date,
        daily_target=daily_target,
        total_target=total_target,
        ledger=ledger,
    )
    return _create_view_actions(session, task, actions=actions, context=context)


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
    *,
    actions: list[tuple[ChannelMessage, int]],
    context: "ViewCreationContext",
) -> int:
    now_value = _now()
    times = schedule_times(
        len(actions),
        task.pacing_config or {},
        start_at=now_value,
        deadline_at=next_local_day_deadline(now_value, task.timezone),
    )
    created = 0
    for index, (message, account_id) in enumerate(actions):
        planned_at = adjust_for_account_hour_limit(
            session,
            task,
            account_id,
            "view_message",
            times[index],
            context.config,
        )
        obligation = ensure_view_obligation(
            session,
            context.ledger,
            message,
            account_id,
        )
        if not obligation_accepts_new_action(obligation):
            continue
        payload = {
            **channel_message_payload(context.channel, message),
            "execution_date": context.execution_date,
            "daily_view_target": context.daily_target,
            "total_view_target": context.total_target,
            "task_day_ledger_id": context.ledger.id,
            "view_fulfillment_obligation_id": obligation.id,
        }
        action = create_view_action(
            session,
            task,
            account_id,
            planned_at,
            ViewMessagePayload(**payload),
        )
        bind_obligation_action(obligation, action)
        created += 1
    return created


@dataclass(frozen=True)
class ViewCreationContext:
    channel: OperationTarget
    config: dict
    execution_date: str
    daily_target: int
    total_target: int
    ledger: TaskDayLedger


def _view_actions_for_messages(
    session: Session,
    task: Task,
    config: dict,
    inputs: "ViewPlanInputs",
    account_ids_by_message: dict[int, set[int]],
) -> list[tuple[ChannelMessage, int]]:
    coverage_remaining = daily_uncovered_account_count(session, task.id, ("view_message",), inputs.accounts)
    actions: list[tuple[ChannelMessage, int]] = []
    task_remaining_today = inputs.task_remaining_today
    for message in inputs.messages:
        if _message_expired(message, config):
            continue
        quantity = _view_quantity_for_message(
            task,
            config,
            inputs,
            message,
            coverage_remaining,
            account_ids_by_message,
        )
        quantity = min(quantity, task_remaining_today)
        if quantity <= 0:
            continue
        candidates = [account for account in inputs.accounts if account.id not in account_ids_by_message[message.id]]
        selected = [account for account in candidates if _account_has_view_daily_capacity(account.id, config, inputs.daily_counts_by_account)][:quantity]
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
    completed_counts: dict[int, int]
    daily_counts_by_account: dict[int, int]
    ledger: TaskDayLedger
    now: datetime


def _view_quantity_for_message(
    task: Task,
    config: dict,
    inputs: ViewPlanInputs,
    message: ChannelMessage,
    coverage_remaining: int,
    account_ids_by_message: dict[int, set[int]],
) -> int:
    base = quantity_with_jitter(inputs.daily_target, float(config.get("view_count_jitter") or 0))
    completed_count = inputs.completed_counts.get(message.id, 0)
    if completed_count >= inputs.total_target:
        return 0
    target = min(max(base, coverage_remaining), inputs.total_target - completed_count)
    if task.fulfillment_contract_version == CURRENT_CONTRACT_VERSION:
        target = _current_view_due(task, inputs, message, target)
    used_count = len(account_ids_by_message[message.id])
    return max(0, target - used_count)


def _current_view_due(
    task: Task,
    inputs: ViewPlanInputs,
    message: ChannelMessage,
    target: int,
) -> int:
    period_start = inputs.ledger.period_start_at
    task_anchor = task_pacing_anchor(task)
    anchor = max(task_anchor, message.created_at) if task_anchor else message.created_at
    return cumulative_pacing_due(
        target,
        task.pacing_config or {},
        anchor_at=anchor,
        period_start_at=period_start,
        period_end_at=inputs.ledger.deadline_at,
        now=inputs.now,
    )


def _remaining_task_daily_capacity(daily_cap: int | None, planned_today: int) -> int:
    if daily_cap is None:
        return 100000000
    return max(0, daily_cap - planned_today)


def _account_has_view_daily_capacity(account_id: int, config: dict, daily_counts_by_account: dict[int, int]) -> bool:
    limit = int(config.get("max_views_per_account_per_day") or 0)
    if limit <= 0:
        return True
    return daily_counts_by_account.get(account_id, 0) < limit


def _message_expired(message: ChannelMessage, config: dict) -> bool:
    active_days = int(config.get("message_active_days") or 0)
    if active_days <= 0 or not message.published_at:
        return False
    return message.published_at < _now() - timedelta(days=active_days)


def _empty_view_plan_message(
    task: Task,
    config: dict,
    messages: list[ChannelMessage],
    total_target: int,
    completed_counts: dict[int, int],
) -> str:
    if _view_targets_inactive_or_reached(config, messages, total_target, completed_counts):
        return ""
    return task.last_error or "没有可新增的有效浏览账号"


def _view_targets_inactive_or_reached(
    config: dict,
    messages: list[ChannelMessage],
    total_target: int,
    completed_counts: dict[int, int],
) -> bool:
    if not messages:
        return False
    if all(_message_expired(message, config) for message in messages):
        return True
    return all(completed_counts.get(message.id, 0) >= total_target for message in messages)


__all__ = ["build_plan"]

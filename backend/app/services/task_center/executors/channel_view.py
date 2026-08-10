from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.models import (
    ChannelMessage,
    ChannelViewDailyMessageTarget,
    OperationTarget,
    Task,
    TaskDayLedger,
)
from app.services._common import _now

from ..account_pool import select_task_accounts
from ..channel_fulfillment import (
    bind_obligation_action,
    ensure_view_obligation,
    obligation_accepts_new_action,
    view_account_ids_for_messages,
    view_confirmed_counts,
    view_daily_counts,
    view_materialized_account_ids_for_messages,
)
from ..channel_membership import channel_member_accounts, gate_channel_membership
from ..channel_view_targets import (
    channel_view_target_due,
    ensure_channel_view_targets,
    target_messages,
)
from ..daily_ledgers import ensure_task_day_ledger
from ..pacing import schedule_times
from ..payloads import ViewMessagePayload, create_view_action
from ..schedule_reservation import reserve_task_schedule_times
from .common import adjust_for_account_hour_limit, channel_message_payload, channel_scope, quantity_jitter_bounds, record_channel_capacity_warning


def build_plan(session: Session, task: Task) -> int:
    config = task.type_config or {}
    channel = session.get(OperationTarget, int(config.get("target_channel_id") or 0))
    if not channel or channel.tenant_id != task.tenant_id or channel.target_type != "channel":
        task.last_error = "目标频道不存在"
        return 0
    gate = gate_channel_membership(session, task, channel)
    if not gate.ready:
        return gate.created
    scoped_channel, selected_messages = channel_scope(session, task, config)
    if scoped_channel is not None:
        channel = scoped_channel
    scope = _view_target_scope(
        session,
        task,
        channel,
        selected_messages=selected_messages,
        config=config,
    )
    prepared = _view_plan_inputs(session, task, scope, config=config)
    if prepared is None:
        return 0
    inputs, completed_counts, total_target = prepared
    actions = _view_actions_for_messages(task, config, inputs)
    if not actions:
        task.last_error = _empty_view_plan_message(
            task,
            scope.messages,
            completed_counts,
            config=config,
            total_target=total_target,
        )
        return 0
    context = ViewCreationContext(
        channel=scope.channel,
        config=config,
        execution_date=scope.ledger.obligation_local_date.isoformat(),
        ledger=scope.ledger,
        targets_by_message=scope.targets_by_message,
    )
    return _create_view_actions(session, task, actions=actions, context=context)


def _view_target_scope(
    session: Session,
    task: Task,
    channel: OperationTarget,
    *,
    selected_messages: list[ChannelMessage],
    config: dict,
) -> "ViewTargetScope":
    now_value = _now()
    ledger = ensure_task_day_ledger(session, task, now=now_value)
    targets = ensure_channel_view_targets(
        session,
        task,
        channel,
        ledger=ledger,
        messages=selected_messages,
        config=config,
        now=now_value,
    )
    return ViewTargetScope(
        channel,
        ledger,
        target_messages(session, targets),
        targets,
        now_value,
    )


def _view_plan_inputs(
    session: Session,
    task: Task,
    scope: "ViewTargetScope",
    *,
    config: dict,
) -> tuple["ViewPlanInputs", dict[int, int], int] | None:
    daily_target = int(config.get("per_message_daily_view_target") or config.get("target_views_per_message") or 1)
    total_target = max(daily_target, int(config.get("per_message_total_view_target") or config.get("target_views_per_message") or daily_target))
    if not scope.messages:
        return None
    task_daily_cap = int(config.get("task_daily_view_safety_cap") or 0)
    effective_daily_cap = task_daily_cap if task_daily_cap > 0 else None
    capacity_target = max(target.daily_target_snapshot for target in scope.targets_by_message.values())
    account_ids_by_message = view_account_ids_for_messages(session, task, scope.ledger, scope.messages)
    identity_scan_floor = max(
        capacity_target + len(account_ids)
        for account_ids in account_ids_by_message.values()
    )
    accounts = _view_accounts(
        session,
        task,
        scope.channel,
        config=config,
        target_per_message=capacity_target,
        identity_scan_floor=identity_scan_floor,
    )
    if not accounts:
        task.last_error = "没有可用账号，等待账号恢复后继续执行"
        return None
    record_channel_capacity_warning(task, "浏览", capacity_target, len(accounts))
    daily_counts = view_daily_counts(session, scope.ledger)
    task_remaining_today = _remaining_task_daily_capacity(effective_daily_cap, daily_counts.total)
    if task_remaining_today <= 0:
        task.last_error = "任务今日浏览安全上限已用完，等待下一日继续规划"
        return None
    materialized_ids_by_message = view_materialized_account_ids_for_messages(session, scope.ledger, scope.messages)
    completed_counts = view_confirmed_counts(session, task, scope.messages)
    inputs = ViewPlanInputs(
        messages=scope.messages,
        accounts=accounts,
        task_remaining_today=task_remaining_today,
        daily_counts_by_account=daily_counts.by_account,
        ledger=scope.ledger,
        targets_by_message=scope.targets_by_message,
        lifetime_ids_by_message=account_ids_by_message,
        materialized_ids_by_message=materialized_ids_by_message,
        now=scope.now,
    )
    return inputs, completed_counts, total_target


def _view_accounts(
    session: Session,
    task: Task,
    channel: OperationTarget,
    *,
    config: dict,
    target_per_message: int,
    identity_scan_floor: int,
) -> list:
    task_daily_cap = int(config.get("task_daily_view_safety_cap") or 0)
    _lower, max_target_per_message = quantity_jitter_bounds(
        target_per_message,
        float(config.get("view_count_jitter") or 0),
    )
    account_scan_limit = max(
        max_target_per_message,
        identity_scan_floor,
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
    times = _view_schedule_times(
        session,
        task,
        len(actions),
        deadline_at=context.ledger.deadline_at,
    )
    created = 0
    for (message, account_id), scheduled_at in zip(actions, times, strict=False):
        request = ViewActionRequest(task, message, account_id, scheduled_at, context)
        created += _create_scheduled_view_action(session, request)
    return created


def _view_schedule_times(
    session: Session,
    task: Task,
    count: int,
    *,
    deadline_at: datetime,
) -> list[datetime]:
    times = schedule_times(
        count,
        task.pacing_config or {},
        start_at=_now(),
        deadline_at=deadline_at,
        preserve_minimum_spacing=False,
    )
    return reserve_task_schedule_times(
        session,
        task,
        "view_message",
        times,
        pacing_config=task.pacing_config or {},
        deadline_at=deadline_at,
        enforce_task_spacing=False,
    )


def _create_scheduled_view_action(session: Session, request: "ViewActionRequest") -> int:
    context = request.context
    planned_at = adjust_for_account_hour_limit(
        session,
        request.task,
        request.account_id,
        "view_message",
        request.scheduled_at,
        context.config,
    )
    if planned_at >= context.ledger.deadline_at:
        _record_deadline_capacity_blocker(request.task, planned_at, context.ledger.deadline_at)
        return 0
    obligation = ensure_view_obligation(session, context.ledger, request.message, request.account_id)
    if not obligation_accepts_new_action(obligation):
        return 0
    target = context.targets_by_message[request.message.id]
    payload = _view_action_payload(
        context,
        request.message,
        obligation_id=obligation.id,
        target=target,
    )
    action = create_view_action(
        session,
        request.task,
        request.account_id,
        planned_at,
        ViewMessagePayload(**payload),
    )
    bind_obligation_action(obligation, action)
    return 1


def _view_action_payload(
    context: "ViewCreationContext",
    message: ChannelMessage,
    *,
    obligation_id: int,
    target: ChannelViewDailyMessageTarget,
) -> dict:
    return {
        **channel_message_payload(context.channel, message),
        "execution_date": context.execution_date,
        "daily_view_target": target.daily_target_snapshot,
        "total_view_target": target.total_target_snapshot,
        "task_day_ledger_id": context.ledger.id,
        "view_fulfillment_obligation_id": obligation_id,
    }


def _record_deadline_capacity_blocker(
    task: Task,
    planned_at: datetime,
    deadline_at: datetime,
) -> None:
    stats = dict(task.stats or {})
    key = "channel_view_deadline_capacity_defer_count"
    stats[key] = int(stats.get(key) or 0) + 1
    stats["channel_view_deadline_capacity_defer"] = {
        "planned_at": planned_at.isoformat(),
        "deadline_at": deadline_at.isoformat(),
        "reason_code": "account_capacity_after_ledger_deadline",
    }
    task.stats = stats
    task.last_error = "账号容量可用时刻已越过当前浏览任务日截止时间，未创建跨日 Action"


@dataclass(frozen=True)
class ViewTargetScope:
    channel: OperationTarget
    ledger: TaskDayLedger
    messages: list[ChannelMessage]
    targets_by_message: dict[int, ChannelViewDailyMessageTarget]
    now: datetime


@dataclass(frozen=True)
class ViewCreationContext:
    channel: OperationTarget
    config: dict
    execution_date: str
    ledger: TaskDayLedger
    targets_by_message: dict[int, ChannelViewDailyMessageTarget]


@dataclass(frozen=True)
class ViewActionRequest:
    task: Task
    message: ChannelMessage
    account_id: int
    scheduled_at: datetime
    context: ViewCreationContext


def _view_actions_for_messages(
    task: Task,
    config: dict,
    inputs: "ViewPlanInputs",
) -> list[tuple[ChannelMessage, int]]:
    actions: list[tuple[ChannelMessage, int]] = []
    task_remaining_today = inputs.task_remaining_today
    for message in inputs.messages:
        quantity = _view_quantity_for_message(
            task,
            inputs,
            message,
            config=config,
        )
        quantity = min(quantity, task_remaining_today)
        if quantity <= 0:
            continue
        lifetime_ids = inputs.lifetime_ids_by_message[message.id]
        candidates = [
            account for account in inputs.accounts if account.id not in lifetime_ids
        ]
        selected = [account for account in candidates if _account_has_view_daily_capacity(account.id, config, inputs.daily_counts_by_account)][:quantity]
        actions.extend((message, account.id) for account in selected)
        task_remaining_today -= len(selected)
        if task_remaining_today <= 0:
            break
    return actions


@dataclass(frozen=True)
class ViewPlanInputs:
    messages: list[ChannelMessage]
    accounts: list
    task_remaining_today: int
    daily_counts_by_account: dict[int, int]
    ledger: TaskDayLedger
    targets_by_message: dict[int, ChannelViewDailyMessageTarget]
    lifetime_ids_by_message: dict[int, set[int]]
    materialized_ids_by_message: dict[int, set[int]]
    now: datetime


def _view_quantity_for_message(
    task: Task,
    inputs: ViewPlanInputs,
    message: ChannelMessage,
    *,
    config: dict,
) -> int:
    target_row = inputs.targets_by_message[message.id]
    target = channel_view_target_due(
        target_row,
        inputs.ledger,
        task.pacing_config or {},
        now=inputs.now,
    )
    baseline = int(target_row.ledger_confirmed_at_attach or 0)
    used_count = max(0, len(inputs.materialized_ids_by_message[message.id]) - baseline)
    return max(0, target - used_count)


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
    messages: list[ChannelMessage],
    completed_counts: dict[int, int],
    *,
    config: dict,
    total_target: int,
) -> str:
    if _view_targets_inactive_or_reached(
        messages,
        completed_counts,
        config=config,
        total_target=total_target,
    ):
        return ""
    return task.last_error or "没有可新增的有效浏览账号"


def _view_targets_inactive_or_reached(
    messages: list[ChannelMessage],
    completed_counts: dict[int, int],
    *,
    config: dict,
    total_target: int,
) -> bool:
    if not messages:
        return False
    if all(_message_expired(message, config) for message in messages):
        return True
    return all(completed_counts.get(message.id, 0) >= total_target for message in messages)


__all__ = ["build_plan"]

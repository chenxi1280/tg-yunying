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
    ViewFulfillmentObligation,
)
from app.services._common import _now
from app.timezone import as_beijing

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
from ..channel_view_daily_identity import (
    DailyIdentityClaim,
    bind_daily_identity_action,
    claim_daily_identity,
    release_claimed_identity,
)
from ..channel_membership import channel_member_accounts, gate_channel_membership
from ..channel_view_capacity import record_unique_account_capacity
from ..channel_view_targets import (
    ensure_channel_view_targets,
    refresh_channel_view_targets,
    target_messages,
)
from ..daily_ledgers import ensure_task_day_ledger
from ..datetime_compat import utc_storage_as_beijing_wall
from ..fulfillment_activation import CURRENT_CONTRACT_VERSION
from ..pacing import schedule_due_times, schedule_times
from ..payloads import ViewMessagePayload, create_view_action
from ..schedule_reservation import reserve_task_schedule_times
from ..config_normalization import apply_group_ai_account_coverage_defaults
from .channel_view_pacing import (
    ViewActionRequest,
    ViewCreationContext,
    bind_view_action_pacing,
    create_current_view_actions,
    record_view_deadline_capacity_blocker as _record_deadline_capacity_blocker,
    reserve_view_action_pacing,
)
from .channel_view_allocation import (
    ViewPlanInputs,
    record_unique_capacity as _record_unique_capacity,
    view_actions_for_messages as _view_actions_for_messages,
    view_quantity_for_message as _view_quantity_for_message,
)
from .common import adjust_for_account_hour_limit, channel_message_payload, channel_scope, quantity_jitter_bounds, record_channel_capacity_warning


def effective_channel_view_config(task: Task) -> dict:
    return apply_group_ai_account_coverage_defaults(
        task.type,
        dict(task.type_config or {}),
        task.account_config or {},
    )


def build_plan(session: Session, task: Task) -> int:
    config = effective_channel_view_config(task)
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
        if selected_messages and all(_message_expired(message, config) for message in selected_messages):
            task.last_error = ""
        return 0
    inputs, completed_counts, total_target = prepared
    actions = _view_actions_for_messages(task, config, inputs)
    unique_capacity_shortfall = _record_unique_capacity(task, inputs, config=config)
    if not actions:
        task.last_error = (
            "channel_view_unique_account_capacity_shortfall"
            if unique_capacity_shortfall
            else _empty_view_plan_message(
                task,
                scope.messages,
                completed_counts,
                config=config,
                total_target=total_target,
            )
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
    record_unique_account_capacity(task, ())
    if not scope.messages:
        return None
    task_daily_cap = int(config.get("task_daily_view_safety_cap") or 0)
    effective_daily_cap = task_daily_cap if task_daily_cap > 0 else None
    accounts, account_ids_by_message, capacity_target = _view_account_plan(
        session,
        task,
        scope,
        config=config,
    )
    if not accounts:
        task.last_error = "没有可用账号，等待账号恢复后继续执行"
        return None
    account_pool_size = len(accounts)
    total_target = _refresh_view_target_capacity(
        task,
        scope,
        config,
        account_pool_size=account_pool_size,
    )
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


def _view_account_plan(
    session: Session,
    task: Task,
    scope: "ViewTargetScope",
    *,
    config: dict,
) -> tuple[list, dict[int, set[int]], int]:
    capacity_target = max(
        (target.daily_target_snapshot for target in scope.targets_by_message.values()),
        default=1,
    )
    account_ids_by_message = view_account_ids_for_messages(session, task, scope.ledger, scope.messages)
    identity_scan_floor = max(
        (capacity_target + len(account_ids) for account_ids in account_ids_by_message.values()),
        default=capacity_target,
    )
    accounts = _view_accounts(
        session,
        task,
        scope.channel,
        config=config,
        target_per_message=capacity_target,
        identity_scan_floor=identity_scan_floor,
    )
    return accounts, account_ids_by_message, capacity_target


def _refresh_view_target_capacity(
    task: Task,
    scope: "ViewTargetScope",
    config: dict,
    *,
    account_pool_size: int,
) -> int:
    _daily_target, total_target = _view_target_limits(config, candidate_count=account_pool_size)
    refresh_channel_view_targets(
        scope.targets_by_message,
        scope.ledger,
        task.pacing_config or {},
        now=scope.now,
        candidate_account_count=account_pool_size,
        config=config,
    )
    return total_target


def _view_target_limits(config: dict, candidate_count: int | None = None) -> tuple[int, int]:
    configured_daily = config.get("per_message_daily_view_target") or config.get("target_views_per_message")
    if configured_daily is not None and str(configured_daily).strip() != "" and int(configured_daily) > 0:
        daily = int(configured_daily)
    elif candidate_count is not None and candidate_count > 0:
        daily = candidate_count
    else:
        daily = 1
    raw_total = config.get("per_message_total_view_target")
    if raw_total is None or str(raw_total).strip() == "" or int(raw_total) <= 0:
        total = 0
    else:
        total = max(daily, int(raw_total))
    return daily, total


def _view_accounts(
    session: Session,
    task: Task,
    channel: OperationTarget,
    *,
    config: dict,
    target_per_message: int,
    identity_scan_floor: int,
) -> list:
    daily_coverage = config.get("account_coverage_mode") == "all_accounts_daily"
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
            limit=None if daily_coverage else account_scan_limit,
            enforce_max_concurrent=False,
            scan_all_candidates=daily_coverage,
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
    if getattr(task, "fulfillment_contract_version", "") == CURRENT_CONTRACT_VERSION:
        return create_current_view_actions(
            session,
            task,
            actions=actions,
            context=context,
            action_creator=_create_scheduled_view_action,
        )
    times = _view_schedule_times(session, task, len(actions), deadline_at=context.ledger.deadline_at)
    created = 0
    for (message, account_id), due_at in zip(actions, times, strict=False):
        request = ViewActionRequest(
            task,
            message,
            account_id,
            due_at,
            context,
        )
        created += _create_scheduled_view_action(session, request)
    return created


def _view_schedule_times(
    session: Session,
    task: Task,
    count: int,
    *,
    deadline_at: datetime,
) -> list[datetime]:
    now_value = _now()
    local_deadline = _ledger_deadline_for_planned_at(deadline_at, now_value)
    if getattr(task, "fulfillment_contract_version", "") == CURRENT_CONTRACT_VERSION:
        return schedule_due_times(
            count,
            task.pacing_config or {},
            start_at=now_value,
            deadline_at=local_deadline,
            timezone_name=task.timezone,
            seed_id=f"view:{task.id}",
            plan_total=count,
        )
    times = schedule_times(
        count,
        task.pacing_config or {},
        start_at=now_value,
        deadline_at=local_deadline,
        preserve_minimum_spacing=False,
    )
    return reserve_task_schedule_times(
        session,
        task,
        "view_message",
        times,
        pacing_config=task.pacing_config or {},
        deadline_at=local_deadline,
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
    deadline_at = request.deadline_at or _ledger_deadline_for_planned_at(context.ledger.deadline_at, planned_at)
    if planned_at >= deadline_at:
        _record_deadline_capacity_blocker(request.task, planned_at, deadline_at)
        return 0
    obligation = request.obligation or ensure_view_obligation(
        session, context.ledger, request.message, request.account_id,
    )
    if not obligation_accepts_new_action(obligation):
        return 0
    if not _claim_daily_identity_for_request(session, request, obligation):
        return 0
    schedule = reserve_view_action_pacing(
        session, request, planned_at=planned_at, deadline_at=deadline_at,
    )
    if schedule is None:
        release_claimed_identity(session, obligation)
        return 0
    planned_at, reservation = schedule
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
    bind_view_action_pacing(action, request, obligation, reservation)
    bind_obligation_action(obligation, action)
    bind_daily_identity_action(session, obligation, action)
    return 1


def _claim_daily_identity_for_request(
    session: Session,
    request: "ViewActionRequest",
    obligation: ViewFulfillmentObligation,
) -> bool:
    context = request.context
    owner = claim_daily_identity(
        session,
        DailyIdentityClaim(
            tenant_id=request.task.tenant_id,
            logical_task_id=request.task.id,
            target_peer_id=context.channel.tg_peer_id,
            channel_message_id=request.message.id,
            account_id=request.account_id,
            obligation_local_date=context.ledger.obligation_local_date,
            obligation_id=obligation.id,
        ),
    )
    if owner is not None:
        return True
    _record_daily_identity_conflict(request.task)
    return False


def _record_daily_identity_conflict(task: Task) -> None:
    stats = dict(task.stats or {})
    key = "channel_view_daily_identity_conflict_count"
    stats[key] = int(stats.get(key) or 0) + 1
    task.stats = stats
    task.last_error = "同日同账号同帖子已由其他任务占用，未创建重复浏览 Action"


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


def _ledger_deadline_for_planned_at(
    deadline_at: datetime,
    planned_at: datetime,
) -> datetime:
    local_wall = utc_storage_as_beijing_wall(deadline_at)
    if planned_at.tzinfo is None:
        return local_wall
    return local_wall.replace(tzinfo=planned_at.tzinfo)


@dataclass(frozen=True)
class ViewTargetScope:
    channel: OperationTarget
    ledger: TaskDayLedger
    messages: list[ChannelMessage]
    targets_by_message: dict[int, ChannelViewDailyMessageTarget]
    now: datetime


def _remaining_task_daily_capacity(daily_cap: int | None, planned_today: int) -> int:
    if daily_cap is None:
        return 100000000
    return max(0, daily_cap - planned_today)


def _message_expired(message: ChannelMessage, config: dict) -> bool:
    active_days = int(config.get("message_active_days") or 0)
    if active_days <= 0 or not message.published_at:
        return False
    published_at = as_beijing(message.published_at)
    return published_at < as_beijing(_now()) - timedelta(days=active_days)


def _empty_view_plan_message(
    task: Task,
    messages: list[ChannelMessage],
    completed_counts: dict[int, int],
    *,
    config: dict,
    total_target: int,
) -> str:
    if not messages:
        return task.last_error or "未找到频道消息，等待下一轮采集"
    if all(_message_expired(message, config) for message in messages):
        return ""
    if total_target > 0 and all(completed_counts.get(message.id, 0) >= total_target for message in messages):
        return ""
    return task.last_error or "没有可新增的有效浏览账号"


__all__ = ["build_plan"]

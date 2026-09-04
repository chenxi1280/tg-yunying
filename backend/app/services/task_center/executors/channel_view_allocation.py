from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.models import ChannelMessage, ChannelViewDailyMessageTarget, Task, TaskDayLedger
from app.services._common import _now

from ..channel_view_capacity import record_unique_account_capacity
from ..channel_view_targets import channel_view_target_due
from .channel_view_pacing import effective_channel_view_pacing_config


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
    allowed_account_ids_by_message: dict[int, set[int]] | None
    now: datetime


def view_actions_for_messages(
    task: Task,
    config: dict,
    inputs: ViewPlanInputs,
) -> list[tuple[ChannelMessage, int]]:
    daily_counts = dict(inputs.daily_counts_by_account)
    messages_with_quotas, eligible_accounts = _message_quotas(
        task,
        inputs,
        config=config,
        daily_counts=daily_counts,
    )
    if not messages_with_quotas:
        return []
    unassigned = [
        account for account in inputs.accounts
        if daily_counts.get(account.id, 0) == 0
    ]
    matched = _match_distinct_accounts_to_messages(
        messages_with_quotas,
        eligible_accounts,
        unassigned,
    )
    return _allocated_actions(
        messages_with_quotas,
        eligible_accounts=eligible_accounts,
        matched_by_message=matched,
        config=config,
        initial_daily_counts=daily_counts,
        action_limit=inputs.task_remaining_today,
    )


def _message_quotas(
    task: Task,
    inputs: ViewPlanInputs,
    *,
    config: dict,
    daily_counts: dict[int, int],
) -> tuple[list[tuple[ChannelMessage, int]], dict[int, list]]:
    quotas: list[tuple[ChannelMessage, int]] = []
    eligible_by_message: dict[int, list] = {}
    messages = sorted(
        inputs.messages,
        key=lambda message: (
            message.published_at or message.created_at or _now(),
            message.id,
        ),
        reverse=True,
    )
    for message in messages:
        quantity = view_quantity_for_message(task, inputs, message, config=config)
        quantity = min(quantity, inputs.task_remaining_today)
        eligible = _eligible_accounts(
            message,
            inputs,
            config=config,
            daily_counts=daily_counts,
        )
        if quantity <= 0 or not eligible:
            continue
        quotas.append((message, quantity))
        eligible_by_message[message.id] = eligible
    return quotas, eligible_by_message


def _eligible_accounts(
    message: ChannelMessage,
    inputs: ViewPlanInputs,
    *,
    config: dict,
    daily_counts: dict[int, int],
) -> list:
    lifetime_ids = inputs.lifetime_ids_by_message[message.id]
    return [
        account for account in inputs.accounts
        if account.id not in lifetime_ids
        and _allowed_by_frozen_edge(inputs, message.id, account.id)
        and _account_has_view_daily_capacity(account.id, config, daily_counts)
    ]


def _allowed_by_frozen_edge(
    inputs: ViewPlanInputs, message_id: int, account_id: int
) -> bool:
    if inputs.allowed_account_ids_by_message is None:
        return True
    return account_id in inputs.allowed_account_ids_by_message.get(message_id, set())


def _match_distinct_accounts_to_messages(
    messages_with_quotas: list[tuple[ChannelMessage, int]],
    eligible_accounts_by_message: dict[int, list],
    candidate_accounts: list,
) -> dict[int, list]:
    matched: dict[int, list] = {message.id: [] for message, _ in messages_with_quotas}
    quotas = {message.id: quota for message, quota in messages_with_quotas}
    eligible_ids = {
        message_id: {account.id for account in accounts}
        for message_id, accounts in eligible_accounts_by_message.items()
    }
    sorted_accounts = sorted(
        candidate_accounts,
        key=lambda account: (
            sum(account.id in ids for ids in eligible_ids.values()),
            account.id,
        ),
    )

    def find_path(account, visited_messages: set[int]) -> bool:
        for message, _ in messages_with_quotas:
            if message.id in visited_messages or account.id not in eligible_ids[message.id]:
                continue
            visited_messages.add(message.id)
            if len(matched[message.id]) < quotas[message.id]:
                matched[message.id].append(account)
                return True
            for index, matched_account in enumerate(list(matched[message.id])):
                if find_path(matched_account, visited_messages):
                    matched[message.id][index] = account
                    return True
        return False

    for account in sorted_accounts:
        find_path(account, set())
    return matched


def _allocated_actions(
    messages_with_quotas: list[tuple[ChannelMessage, int]],
    *,
    eligible_accounts: dict[int, list],
    matched_by_message: dict[int, list],
    config: dict,
    initial_daily_counts: dict[int, int],
    action_limit: int,
) -> list[tuple[ChannelMessage, int]]:
    actions: list[tuple[ChannelMessage, int]] = []
    daily_counts = dict(initial_daily_counts)
    remaining = action_limit
    for message, quota in messages_with_quotas:
        matched = matched_by_message[message.id]
        selected = matched[:remaining]
        remaining = _append_actions(
            actions,
            message,
            accounts=selected,
            daily_counts=daily_counts,
            remaining=remaining,
        )
        if remaining <= 0:
            break
        candidates = [
            account for account in eligible_accounts[message.id]
            if account not in matched
            and _account_has_view_daily_capacity(account.id, config, daily_counts)
        ]
        fill_count = min(quota - len(matched), remaining)
        remaining = _append_actions(
            actions,
            message,
            accounts=candidates[:fill_count],
            daily_counts=daily_counts,
            remaining=remaining,
        )
    return actions


def _append_actions(
    actions: list[tuple[ChannelMessage, int]],
    message: ChannelMessage,
    *,
    accounts: list,
    daily_counts: dict[int, int],
    remaining: int,
) -> int:
    for account in accounts:
        actions.append((message, account.id))
        daily_counts[account.id] = daily_counts.get(account.id, 0) + 1
        remaining -= 1
    return remaining


def record_unique_capacity(task: Task, inputs: ViewPlanInputs, *, config: dict) -> bool:
    capacities: list[tuple[int, int]] = []
    for message in inputs.messages:
        required = view_quantity_for_message(task, inputs, message, config=config)
        if required <= 0:
            continue
        available = sum(
            account.id not in inputs.lifetime_ids_by_message[message.id]
            and _account_has_view_daily_capacity(
                account.id,
                config,
                inputs.daily_counts_by_account,
            )
            for account in inputs.accounts
        )
        capacities.append((required, available))
    return record_unique_account_capacity(task, capacities)


def view_quantity_for_message(
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
        effective_channel_view_pacing_config(task),
        now=inputs.now,
    )
    baseline = int(target_row.ledger_confirmed_at_attach or 0)
    used_count = max(0, len(inputs.materialized_ids_by_message[message.id]) - baseline)
    return max(0, target - used_count)


def _account_has_view_daily_capacity(
    account_id: int,
    config: dict,
    daily_counts_by_account: dict[int, int],
) -> bool:
    limit = int(config.get("max_views_per_account_per_day") or 0)
    return limit <= 0 or daily_counts_by_account.get(account_id, 0) < limit


__all__ = [
    "ViewPlanInputs",
    "record_unique_capacity",
    "view_actions_for_messages",
    "view_quantity_for_message",
]

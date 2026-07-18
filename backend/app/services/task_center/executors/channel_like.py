from __future__ import annotations

import random

from sqlalchemy.orm import Session

from app.models import ChannelMessage, OperationTarget, Task

from ..account_pool import daily_uncovered_account_count, select_task_accounts
from ..channel_membership import channel_member_accounts, gate_channel_membership
from ..pacing import schedule_times
from ..payloads import LikeMessagePayload, create_like_action
from .channel_action_history import channel_message_account_ids_for_messages
from .common import adjust_for_account_hour_limit, channel_message_payload, channel_scope, quantity_jitter_bounds, quantity_with_jitter, record_channel_capacity_warning

LIKE_UNAVAILABLE_SKIP_CODES = {"reaction_unavailable_message", "reaction_unavailable_sibling"}
PRIMARY_REACTION_RATIO = 0.7
EXTRA_REACTION_RATIO = 0.1
MIN_EXTRA_REACTION_QUANTITY = 10
DEFAULT_EXTRA_REACTIONS = ("👏", "🎉", "😁", "🤩", "👌", "🙏", "💯", "⚡")


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
    reactions = config.get("allowed_reactions") or ["👍"]
    target_per_message = int(config.get("target_likes_per_message") or 1)
    _lower, max_target_per_message = quantity_jitter_bounds(target_per_message, float(config.get("like_count_jitter") or 0))
    account_scan_limit = max(max_target_per_message, int((task.account_config or {}).get("max_concurrent") or max_target_per_message))
    accounts = channel_member_accounts(
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
            daily_coverage_action_types=("like_message",),
        ),
    )
    if not accounts:
        task.last_error = "没有可用账号，等待账号恢复后继续执行"
        return 0
    record_channel_capacity_warning(task, "点赞", target_per_message, len(accounts))
    account_ids_by_message = channel_message_account_ids_for_messages(
        session,
        task,
        "like_message",
        messages,
        include_skipped_codes=LIKE_UNAVAILABLE_SKIP_CODES,
    )
    actions = _like_actions_for_messages(session, task, config, messages, accounts, reactions, target_per_message, account_ids_by_message)
    if not actions:
        task.last_error = _empty_like_plan_message(task, messages, target_per_message, account_ids_by_message)
        return 0
    return _create_like_actions(session, task, channel, config, actions)


def _create_like_actions(
    session: Session,
    task: Task,
    channel: OperationTarget,
    config: dict,
    actions: list[tuple[ChannelMessage, int, str]],
) -> int:
    times = schedule_times(len(actions), task.pacing_config or {})
    created = 0
    for index, (message, account_id, reaction) in enumerate(actions):
        planned_at = times[index]
        planned_at = adjust_for_account_hour_limit(session, task, account_id, "like_message", planned_at, config)
        create_like_action(
            session,
            task,
            account_id,
            planned_at,
            LikeMessagePayload(**channel_message_payload(channel, message), reaction_emoji=reaction),
        )
        created += 1
    return created


def _like_actions_for_messages(
    session: Session,
    task: Task,
    config: dict,
    messages: list[ChannelMessage],
    accounts: list,
    reactions: list[str],
    target_per_message: int,
    account_ids_by_message: dict[int, set[int]],
) -> list[tuple[ChannelMessage, int, str]]:
    coverage_remaining = daily_uncovered_account_count(session, task.id, ("like_message",), accounts)
    actions: list[tuple[ChannelMessage, int, str]] = []
    for message in messages:
        used_accounts = account_ids_by_message[message.id]
        available_accounts = [account for account in accounts if account.id not in used_accounts]
        base_desired = quantity_with_jitter(target_per_message, float(config.get("like_count_jitter") or 0))
        target_deficit = max(0, base_desired - len(used_accounts))
        quantity = min(target_deficit, len(available_accounts))
        message_reactions = _reaction_plan(reactions, quantity, str(config.get("reaction_type") or "random"))
        actions.extend((message, available_accounts[index].id, message_reactions[index]) for index in range(quantity))
        coverage_remaining = max(0, coverage_remaining - quantity)
    return actions


def _empty_like_plan_message(
    task: Task,
    messages: list[ChannelMessage],
    target_per_message: int,
    account_ids_by_message: dict[int, set[int]],
) -> str:
    if _all_like_targets_reached(messages, target_per_message, account_ids_by_message):
        return ""
    return task.last_error or "没有可新增的有效点赞账号"


def _all_like_targets_reached(
    messages: list[ChannelMessage],
    target_per_message: int,
    account_ids_by_message: dict[int, set[int]],
) -> bool:
    target = max(1, int(target_per_message or 1))
    if not messages:
        return False
    return all(len(account_ids_by_message[message.id]) >= target for message in messages)


def _reaction_plan(reactions: list[str], quantity: int, reaction_type: str = "random") -> list[str]:
    normalized = _normalize_reactions(reactions)
    if quantity <= 0:
        return []
    if reaction_type == "specific" or len(normalized) == 1:
        return [normalized[0]] * quantity
    primary_count = _primary_reaction_count(quantity)
    extra_count = _extra_reaction_count(normalized, quantity, primary_count)
    secondary_count = max(0, quantity - primary_count - extra_count)
    plan = [normalized[0]] * primary_count
    plan.extend(_secondary_reactions(normalized[1:], secondary_count))
    plan.extend(_extra_reactions(normalized, extra_count))
    random.shuffle(plan)
    return plan


def _normalize_reactions(reactions: list[str]) -> list[str]:
    normalized: list[str] = []
    for reaction in reactions or []:
        value = str(reaction).strip()
        if value and value not in normalized:
            normalized.append(value)
    return normalized or ["👍"]


def _primary_reaction_count(quantity: int) -> int:
    if quantity <= 1:
        return quantity
    return min(quantity, max(1, round(quantity * PRIMARY_REACTION_RATIO)))


def _extra_reaction_count(reactions: list[str], quantity: int, primary_count: int) -> int:
    extra_pool = [reaction for reaction in DEFAULT_EXTRA_REACTIONS if reaction not in reactions]
    if quantity < MIN_EXTRA_REACTION_QUANTITY or not extra_pool:
        return 0
    secondary_minimum = min(len(reactions) - 1, max(0, quantity - primary_count))
    extra_room = max(0, quantity - primary_count - secondary_minimum)
    return min(extra_room, len(extra_pool), max(1, round(quantity * EXTRA_REACTION_RATIO)))


def _secondary_reactions(reactions: list[str], quantity: int) -> list[str]:
    if quantity <= 0 or not reactions:
        return []
    guaranteed = list(reactions[: min(quantity, len(reactions))])
    remaining = quantity - len(guaranteed)
    selected = guaranteed + random.choices(reactions, k=remaining)
    random.shuffle(selected)
    return selected


def _extra_reactions(configured_reactions: list[str], quantity: int) -> list[str]:
    extra_pool = [reaction for reaction in DEFAULT_EXTRA_REACTIONS if reaction not in configured_reactions]
    return random.sample(extra_pool, k=min(quantity, len(extra_pool)))


__all__ = ["build_plan"]

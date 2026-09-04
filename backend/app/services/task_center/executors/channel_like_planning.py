from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import ChannelMessage, Task

from ..fulfillment_activation import CURRENT_CONTRACT_VERSION
from ..pacing import source_rolling_pacing_due
from ..pacing_quantity import deterministic_quantity_with_jitter, deterministic_rank
from .channel_like_capability import (
    clear_reaction_capability_block,
    message_reaction_plan,
)
from .channel_like_types import LikePlanItem, LikePlanningSpec
from .channel_like_album import album_like_items, logical_like_messages


def like_actions_for_messages(
    session: Session, task: Task, spec: LikePlanningSpec
) -> list[LikePlanItem]:
    actions: list[LikePlanItem] = []
    unified = spec.config.get("engagement_contract_version") == "unified_engagement_v1"
    for message in logical_like_messages(spec.messages) if unified else spec.messages:
        if unified and message.grouped_id:
            actions.extend(album_like_items(session, task, spec, message))
            continue
        actions.extend(_message_actions(session, task, message, spec))
    return actions


def _message_actions(
    session: Session,
    task: Task,
    message: ChannelMessage,
    spec: LikePlanningSpec,
) -> list[LikePlanItem]:
    used_accounts = spec.account_ids_by_message[message.id]
    seed_id = f"like:{task.id}:{message.id}"
    plan_total = deterministic_quantity_with_jitter(
        spec.target_per_message,
        float(spec.config.get("like_count_jitter") or 0),
        seed_id=seed_id,
    )
    if spec.allocated_ids_by_message is not None:
        plan_total = len(spec.allocated_ids_by_message.get(message.id, []))
    selected = _selected_accounts(spec, message.id, seed_id, plan_total)
    if plan_total <= 0:
        return []
    desired = min(plan_total, _paced_target(task, message, plan_total, spec.now))
    if len(used_accounts) >= desired:
        clear_reaction_capability_block(task, message.id)
        return []
    reactions = message_reaction_plan(
        session,
        task,
        message,
        config=spec.config,
        reactions=spec.reactions,
        quantity=plan_total,
        seed_id=seed_id,
    )
    if not reactions:
        return []
    return [
        LikePlanItem(message, account.id, reactions[ordinal], ordinal, plan_total)
        for ordinal, account in enumerate(selected[:desired])
        if account.id not in used_accounts
    ]


def _selected_accounts(
    spec: LikePlanningSpec,
    message_id: int,
    seed_id: str,
    plan_total: int,
) -> list:
    ranked = sorted(
        spec.accounts,
        key=lambda account: deterministic_rank(seed_id, str(account.id)),
    )
    if spec.allocated_ids_by_message is None:
        return ranked[: min(plan_total, len(ranked))]
    accounts_by_id = {account.id: account for account in spec.accounts}
    return [
        accounts_by_id[account_id]
        for account_id in spec.allocated_ids_by_message.get(message_id, [])
        if account_id in accounts_by_id
    ]


def _paced_target(
    task: Task, message: ChannelMessage, target: int, now
) -> int:
    if task.fulfillment_contract_version != CURRENT_CONTRACT_VERSION:
        return target
    return source_rolling_pacing_due(
        target,
        task.pacing_config or {},
        task=task,
        source_observed_at=message.created_at,
        now=now,
    )

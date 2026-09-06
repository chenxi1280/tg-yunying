from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    Action,
    CommentFulfillmentObligation,
    Task,
    TaskCommentCapacityReservation,
)
from app.services._common import _now

from ..account_pacing_guard import (
    AccountPacingDeadlineExceeded,
    AccountPacingLockUnavailable,
    bind_account_pacing_reservation_for_slot,
    release_unbound_account_pacing_reservation,
    reserve_account_pacing,
)
from ..channel_comment_source import comment_source_window
from ..channel_comment_capacity import (
    bind_comment_capacity_action,
    release_comment_capacity,
)
from ..channel_comment_capacity_allocation import rebalance_comment_capacity_epoch
from ..channel_comment_plan_contract import grounding_plan_enabled
from ..daily_ledgers import ensure_task_day_ledger
from ..engagement_portfolio import reserve_portfolio_units
from ..fulfillment_activation import CURRENT_CONTRACT_VERSION
from ..pacing import next_local_day_deadline, schedule_times
from ..pacing_persistence import freeze_action_pacing, freeze_pacing_owner
from ..payloads import PostCommentPayload
from ..schedule_reservation import reserve_task_schedule_times
from ..source_pacing import (
    SourcePacingSlot,
    latest_wall_datetime,
    schedule_source_pacing_points,
    source_pacing_plan_hash,
    wall_datetime,
)
from ..source_capacity_plans import apply_source_capacity_plan
from ..source_owner_cursor import attach_owner_history, pacing_source_key_hash
from .channel_comment_schedule import materialized_reply_slots
from .channel_comment_budget import comment_action_materialization_limit
from .common import (
    adjust_for_account_hour_limit,
    pick_channel_account,
    stats_inc,
)


PreparedCommentAction = tuple[
    int,
    object,
    PostCommentPayload,
    CommentFulfillmentObligation,
]


def bind_prepared_comment_pacing(
    session: Session,
    task: Task,
    action: Action,
    *,
    obligation: CommentFulfillmentObligation,
    account_id: int,
) -> None:
    if task.fulfillment_contract_version != CURRENT_CONTRACT_VERSION:
        return
    slot_key = f"comment:{obligation.id}"
    freeze_action_pacing(action, obligation, slot_key=slot_key)
    bind_account_pacing_reservation_for_slot(
        session,
        tenant_id=task.tenant_id,
        account_id=account_id,
        slot_key=slot_key,
        action=action,
    )
    if grounding_plan_enabled(task):
        bind_comment_capacity_action(session, obligation.id, action.id)


def prepare_comment_actions(
    session: Session,
    task: Task,
    *,
    context: Any,
    slots: list,
    payload_builder: Callable,
) -> list[PreparedCommentAction]:
    now_value = _now()
    requested_count = len(slots)
    slots, planned_times, due_by_slot, release_by_slot, source_slots = _comment_schedule(
        session, task, slots=slots, context=context, now_at=now_value,
    )
    _record_current_schedule_shortfall(task, requested_count, len(planned_times))
    _freeze_scheduled_comment_pacing(
        task, slots, due_by_slot, release_by_slot, source_slots,
    )
    _rebalance_comment_capacity(
        session, task, config=context.config, at=now_value,
    )
    materialized = materialized_reply_slots(
        task,
        slots,
        planned_times,
        now_value=now_value,
    )
    materialized = _fair_materialized_slots(materialized)
    materialized = _apply_materialization_limit(
        session, task, config=context.config, materialized=materialized,
    )
    materialized = _current_epoch_materialized_slots(session, task, materialized)
    prepared: list[PreparedCommentAction] = []
    used_accounts = _active_message_accounts(session, task, slots)
    for index, (slot, planned_at) in enumerate(materialized):
        item = _prepared_slot(
            session,
            task,
            context=context,
            slot=slot,
            planned_at=planned_at,
            account_index=index,
            excluded_account_ids=used_accounts.setdefault(slot.message.id, set()),
            payload_builder=payload_builder,
        )
        if item is not None:
            prepared.append(item)
            used_accounts[slot.message.id].add(item[0])
        elif grounding_plan_enabled(task):
            release_comment_capacity(session, slot.obligation.id)
    return prepared


def _record_current_schedule_shortfall(
    task: Task,
    requested: int,
    scheduled: int,
) -> None:
    if task.fulfillment_contract_version != CURRENT_CONTRACT_VERSION:
        return
    if scheduled < requested:
        _record_comment_shortfall(task, requested, scheduled)


def _rebalance_comment_capacity(
    session: Session,
    task: Task,
    *,
    config: dict,
    at,
) -> None:
    if not grounding_plan_enabled(task):
        return
    rebalance_comment_capacity_epoch(
        session, task,
        daily_cap=int(config.get("daily_comment_cap") or 0), at=at,
    )


def _current_epoch_materialized_slots(
    session: Session,
    task: Task,
    materialized: list[tuple[Any, Any]],
) -> list[tuple[Any, Any]]:
    if not grounding_plan_enabled(task):
        return materialized
    obligation_ids = [slot.obligation.id for slot, _planned_at in materialized]
    reserved_ids = set(session.scalars(select(
        TaskCommentCapacityReservation.obligation_id,
    ).where(
        TaskCommentCapacityReservation.obligation_id.in_(obligation_ids),
        TaskCommentCapacityReservation.reservation_state == "plan_reserved",
    )))
    return [
        item for item in materialized if item[0].obligation.id in reserved_ids
    ]


def _freeze_scheduled_comment_pacing(
    task: Task,
    slots: list,
    due_by_slot: dict[str, object],
    release_by_slot: dict[str, object],
    source_slots: list[SourcePacingSlot],
) -> None:
    for slot in slots:
        slot_key = _comment_slot_key(slot)
        if slot_key not in due_by_slot:
            continue
        _freeze_comment_pacing(
            task, source_slots, slot=slot,
            due_at=due_by_slot[slot_key],
            release_not_before_at=release_by_slot[slot_key],
        )


def _fair_materialized_slots(materialized: list[tuple[Any, Any]]) -> list[tuple[Any, Any]]:
    return sorted(
        materialized,
        key=lambda item: (
            int(item[0].obligation.target_ordinal),
            item[0].message.published_at or item[0].message.created_at,
            int(item[0].message.id),
        ),
    )


def _apply_materialization_limit(
    session: Session,
    task: Task,
    *,
    config: dict,
    materialized: list[tuple[Any, Any]],
) -> list[tuple[Any, Any]]:
    if not grounding_plan_enabled(task):
        return materialized
    limit = comment_action_materialization_limit(session, task, config)
    if limit is None or limit >= len(materialized):
        return materialized
    stats = dict(task.stats or {})
    stats["comment_action_materialization_deferred"] = len(materialized) - max(0, limit)
    task.stats = stats
    return materialized[:max(0, limit)]


def _active_message_accounts(session: Session, task: Task, slots: list) -> dict[int, set[int]]:
    result = {int(slot.message.id): set() for slot in slots}
    actions = session.scalars(select(Action).where(
        Action.task_id == task.id,
        Action.action_type == "post_comment",
        Action.account_id.is_not(None),
        Action.status.in_(("pending", "claiming", "executing", "success", "unknown_after_send")),
    ))
    for action in actions:
        payload = action.payload if isinstance(action.payload, dict) else {}
        message_id = int(payload.get("channel_message_id") or 0)
        if message_id in result:
            result[message_id].add(int(action.account_id))
    return result


def _comment_schedule(
    session: Session,
    task: Task,
    *,
    slots: list,
    context: Any,
    now_at,
) -> tuple[list, list, dict[str, object], dict[str, object], list[SourcePacingSlot]]:
    if task.fulfillment_contract_version == CURRENT_CONTRACT_VERSION:
        return _current_comment_schedule(
            session,
            task,
            slots=slots,
            context=context,
            now_at=now_at,
        )
    deadline = next_local_day_deadline(now_at, task.timezone)
    times = schedule_times(
        len(slots), task.pacing_config or {}, start_at=now_at,
        deadline_at=deadline, preserve_minimum_spacing=True,
    )
    reserved = reserve_task_schedule_times(
        session, task, "post_comment", times,
        pacing_config=task.pacing_config or {}, deadline_at=deadline,
    )
    due_by_slot = {
        _comment_slot_key(slot): due_at
        for slot, due_at in zip(slots, reserved, strict=False)
    }
    return slots, reserved, due_by_slot, dict(due_by_slot), []


def _current_comment_schedule(
    session: Session,
    task: Task,
    *,
    slots: list,
    context: Any,
    now_at,
) -> tuple[list, list, dict[str, object], dict[str, object], list[SourcePacingSlot]]:
    sources = _comment_source_slots(
        session,
            task,
            slots,
            config=context.config,
            source_hash=pacing_source_key_hash(context.channel.tg_peer_id),
    )
    sources = attach_owner_history(
        session,
        task,
        sources,
        owner_model=CommentFulfillmentObligation,
        config=task.pacing_config or {},
        seed_id=f"comment:{task.id}",
    )
    points = schedule_source_pacing_points(
        sources, task.pacing_config or {}, now_at=wall_datetime(now_at),
        timezone_name=task.timezone, seed_id=f"comment:{task.id}",
    )
    points, sources = apply_source_capacity_plan(
        session,
        task,
        sources,
        points=points,
        pacing_domain="comment",
    )
    scheduled = [slot for slot in slots if _comment_slot_key(slot) in points]
    due = {key: point.due_at for key, point in points.items()}
    releases = {key: point.release_not_before_at for key, point in points.items()}
    times = [releases[_comment_slot_key(slot)] for slot in scheduled]
    return scheduled, times, due, releases, sources


def _freeze_comment_pacing(
    task: Task,
    source_slots: list[SourcePacingSlot],
    *,
    slot: Any,
    due_at,
    release_not_before_at,
) -> None:
    if task.fulfillment_contract_version != CURRENT_CONTRACT_VERSION:
        return
    source = _comment_source_slot(source_slots, slot)
    freeze_pacing_owner(
        slot.obligation,
        plan_hash=source_pacing_plan_hash(
            source, task.pacing_config or {}, seed_id=f"comment:{task.id}",
        ),
        slot_ordinal=source.slot_ordinal,
        plan_total=source.plan_total,
        due_at=due_at,
        release_not_before_at=release_not_before_at,
        source_identity=source.owner_identity,
    )
    if source.source_capacity_plan_hash:
        slot.obligation.source_capacity_plan_hash = source.source_capacity_plan_hash
        slot.obligation.source_capacity_slot_ordinal = source.source_capacity_slot_ordinal


def _record_comment_shortfall(task: Task, requested: int, scheduled: int) -> None:
    """来源滚动窗口内无合法节奏窗口时守恒可见：不压缩追量，记录 typed shortfall。"""
    stats = dict(task.stats or {})
    stats["pacing_schedule_shortfall_count"] = int(stats.get("pacing_schedule_shortfall_count") or 0) + (requested - scheduled)
    stats["pacing_schedule_shortfall"] = {
        "reason_code": "pacing_capacity_shortfall",
        "requested": requested,
        "scheduled": scheduled,
    }
    task.stats = stats
    if scheduled == 0:
        task.last_error = "来源滚动窗口内无合法节奏窗口可安排评论义务，形成 pacing shortfall"


def _comment_source_slots(
    session: Session,
    task: Task,
    slots: list,
    *,
    config: dict,
    source_hash: str,
) -> list[SourcePacingSlot]:
    target = int(config.get("target_comments_per_message") or 1)
    jitter = float(config.get("comment_count_jitter") or 0)
    max_target = max(1, round(target * (1 + jitter)))
    totals: dict[int, int] = {}
    result: list[SourcePacingSlot] = []
    for slot in slots:
        message_id = int(slot.obligation.channel_message_id)
        if message_id not in totals:
            totals[message_id] = _comment_plan_total(
                session, task, message_id=message_id, max_target=max_target,
            )
        source_window = comment_source_window(task, slot.message)
        if source_window is None:
            continue
        period_start, deadline = source_window
        result.append(_comment_source_pacing_slot(
            task, slot, message_id=message_id, plan_total=totals[message_id],
            period_start=period_start, deadline=deadline, source_hash=source_hash,
        ))
    return result


def _comment_plan_total(
    session: Session,
    task: Task,
    *,
    message_id: int,
    max_target: int,
) -> int:
    frozen_max = session.scalar(
        select(func.max(CommentFulfillmentObligation.target_ordinal)).where(
            CommentFulfillmentObligation.task_id == task.id,
            CommentFulfillmentObligation.channel_message_id == message_id,
        )
    ) or 0
    return max(max_target, int(frozen_max))


def _comment_source_pacing_slot(
    task: Task,
    slot: Any,
    *,
    message_id: int,
    plan_total: int,
    period_start,
    deadline,
    source_hash: str,
) -> SourcePacingSlot:
    obligation = slot.obligation
    ordinal = (
        int(obligation.pacing_slot_ordinal)
        if obligation.pacing_slot_ordinal is not None
        else int(obligation.target_ordinal) - 1
    )
    frozen_total = (
        int(obligation.pacing_plan_total)
        if obligation.pacing_due_at is not None and obligation.pacing_plan_total
        else plan_total
    )
    return SourcePacingSlot(
        source_key=str(message_id), slot_key=_comment_slot_key(slot),
        slot_ordinal=ordinal, plan_total=frozen_total,
        period_start_at=period_start, deadline_at=deadline,
        release_not_before_at=obligation.release_not_before_at,
        frozen_due_at=obligation.pacing_due_at, owner_id=obligation.id,
        task_lifecycle_epoch=int(task.task_lifecycle_epoch or 1),
        pacing_period_key=f"message:{message_id}",
        pacing_source_key_hash=source_hash,
        source_capacity_plan_hash=obligation.source_capacity_plan_hash,
        source_capacity_slot_ordinal=obligation.source_capacity_slot_ordinal,
    )


def _comment_source_slot(source_slots: list[SourcePacingSlot], slot: Any) -> SourcePacingSlot:
    slot_key = _comment_slot_key(slot)
    return next(source for source in source_slots if source.slot_key == slot_key)


def _comment_slot_key(slot: Any) -> str:
    return f"comment:{slot.obligation.id}"


def _prepared_slot(
    session: Session,
    task: Task,
    *,
    context: Any,
    slot: Any,
    planned_at: Any,
    account_index: int,
    payload_builder: Callable,
    excluded_account_ids: set[int] | None = None,
) -> PreparedCommentAction | None:
    available_accounts = _comment_candidate_accounts(
        task,
        context=context,
        slot=slot,
        planned_at=planned_at,
        account_index=account_index,
        excluded_account_ids=excluded_account_ids or set(),
    )
    if not available_accounts:
        stats_inc(task, "distinct_account_capacity_shortfall")
        return None
    return _pick_schedulable_comment_account(
        session,
        task,
        context=context,
        slot=slot,
        planned_at=planned_at,
        account_index=account_index,
        candidates=available_accounts,
        payload_builder=payload_builder,
    )


def _comment_candidate_accounts(
    task: Task,
    *,
    context: Any,
    slot: Any,
    planned_at: Any,
    account_index: int,
    excluded_account_ids: set[int],
) -> list:
    relation_accounts = _relation_accounts(
        context.accounts, getattr(slot, "reply_target", None),
    )
    msg_time = slot.message.published_at or slot.message.created_at
    is_multi_day = bool(
        msg_time and planned_at and (planned_at - msg_time).total_seconds() >= 86400
    )
    returning = (
        bool((task.type_config or {}).get("allow_returning_accounts"))
        and is_multi_day
        and account_index % 3 == 0
        and bool(excluded_account_ids)
    )
    if returning:
        preferred = [
            account for account in relation_accounts
            if account.id in excluded_account_ids
        ]
        return preferred or relation_accounts
    return _available_accounts(
        relation_accounts,
        bound_account_id=int(slot.obligation.account_id or 0),
        excluded=excluded_account_ids,
    )


def _pick_schedulable_comment_account(
    session: Session,
    task: Task,
    *,
    context: Any,
    slot: Any,
    planned_at: Any,
    account_index: int,
    candidates: list,
    payload_builder: Callable,
) -> PreparedCommentAction | None:
    if task.fulfillment_contract_version != CURRENT_CONTRACT_VERSION:
        return _legacy_prepared_comment(
            session, task, context=context, slot=slot, planned_at=planned_at,
            account_index=account_index, candidates=candidates,
            payload_builder=payload_builder,
        )
    remaining = list(candidates)
    while remaining:
        account = pick_channel_account(
            session, task, remaining, "post_comment", planned_at,
            context.config, account_index,
        )
        if account is None:
            break
        reservation = _comment_pacing_reservation(
            session, task, context=context, slot=slot,
            account_id=account.id, planned_at=planned_at,
        )
        if reservation is None:
            remaining = _without_account(remaining, account.id)
            continue
        if _reserve_comment_portfolio(
            session, task, slot=slot, planned_at=planned_at,
            account_id=account.id,
        ):
            return (
                account.id,
                reservation.effective_claim_at,
                payload_builder(task, context, slot, account_id=account.id),
                slot.obligation,
            )
        release_unbound_account_pacing_reservation(reservation)
        remaining = _without_account(remaining, account.id)
    stats_inc(task, "portfolio_capacity_shortfall")
    return None


def _legacy_prepared_comment(
    session: Session,
    task: Task,
    *,
    context: Any,
    slot: Any,
    planned_at: Any,
    account_index: int,
    candidates: list,
    payload_builder: Callable,
) -> PreparedCommentAction | None:
    account = pick_channel_account(
        session, task, candidates, "post_comment", planned_at,
        context.config, account_index,
    )
    if account is None:
        stats_inc(task, "failure_count")
        return None
    adjusted_at = adjust_for_account_hour_limit(
        session, task, account.id, "post_comment", planned_at, context.config,
    )
    return (
        account.id,
        adjusted_at,
        payload_builder(task, context, slot, account_id=account.id),
        slot.obligation,
    )


def _without_account(accounts: list, account_id: int) -> list:
    return [account for account in accounts if int(account.id) != int(account_id)]


def _reserve_comment_portfolio(
    session: Session,
    task: Task,
    *,
    slot: Any,
    planned_at: Any,
    account_id: int,
) -> bool:
    if str(
        (task.type_config or {}).get("engagement_contract_version") or ""
    ) != "unified_engagement_v1":
        return True
    ledger = ensure_task_day_ledger(session, task, now=planned_at)
    decision = reserve_portfolio_units(
        session,
        task,
        ledger,
        action_class="authored_comment",
        demand_identity=f"comment:{slot.obligation.id}",
        requested_units_by_account={account_id: 1},
    )
    if decision.allocated_units_by_account.get(account_id) == 1:
        return True
    decision.plan.state = "candidate_rejected"
    return False


def _relation_accounts(accounts: list, reply_target: object) -> list:
    target_author_id = (
        reply_target.get("author_account_id")
        if isinstance(reply_target, dict)
        else None
    )
    if not target_author_id:
        return accounts
    return [account for account in accounts if account.id != target_author_id]


def _available_accounts(
    accounts: list,
    *,
    bound_account_id: int,
    excluded: set[int],
) -> list:
    return [
        account for account in accounts
        if account.id not in excluded
        and (not bound_account_id or account.id == bound_account_id)
    ]


def _comment_pacing_reservation(
    session: Session,
    task: Task,
    *,
    context: Any,
    slot: Any,
    account_id: int,
    planned_at,
):
    adjusted_at = adjust_for_account_hour_limit(
        session,
        task,
        account_id,
        "post_comment",
        planned_at,
        context.config,
    )
    due_at = slot.obligation.pacing_due_at or planned_at
    source_window = comment_source_window(task, slot.message)
    if source_window is None:
        return None
    _period_start, deadline = source_window
    try:
        return reserve_account_pacing(
            session, tenant_id=task.tenant_id, task_id=task.id,
            account_id=account_id, slot_key=_comment_slot_key(slot), due_at=due_at,
            release_not_before_at=latest_wall_datetime(
                slot.obligation.release_not_before_at or due_at,
                adjusted_at,
            ),
            deadline_at=deadline,
            engagement_contract_version=str(
                (task.type_config or {}).get("engagement_contract_version") or ""
            ),
            action_class="authored_comment",
        )
    except (AccountPacingDeadlineExceeded, AccountPacingLockUnavailable):
        stats_inc(task, "account_timeline_conflict")
        return None


__all__ = ["bind_prepared_comment_pacing", "prepare_comment_actions"]

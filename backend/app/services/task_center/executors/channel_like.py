from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    ChannelMessage,
    ChannelMessageSourceRevision,
    OperationTarget,
    ReactionFulfillmentObligation,
    Task,
    TgAccount,
)
from app.schemas.task_center import DEFAULT_CHANNEL_LIKE_ALLOWED_REACTIONS
from app.services._common import _now

from ..account_pacing_guard import (
    AccountPacingDeadlineExceeded,
    bind_account_pacing_reservation,
    reserve_account_pacing,
)
from ..account_pool import select_task_accounts
from ..channel_fulfillment import (
    bind_obligation_action,
    ensure_reaction_obligation,
    frozen_reaction_obligation,
    obligation_accepts_new_action,
    reaction_account_ids_for_messages,
)
from ..channel_membership import channel_member_accounts, gate_channel_membership
from ..daily_ledgers import ensure_task_day_ledger
from ..engagement_reaction_capacity import (
    allocated_account_ids_by_message,
    ensure_reaction_capacity_epoch,
    reaction_admissible_account_ids,
)
from ..fulfillment_activation import CURRENT_CONTRACT_VERSION
from ..pacing import next_local_day_deadline, schedule_times
from ..pacing_persistence import freeze_action_pacing, freeze_pacing_owner
from ..payloads import LikeMessagePayload, create_like_action
from ..schedule_reservation import reserve_task_schedule_times
from ..source_pacing import (
    SourcePacingSlot,
    latest_wall_datetime,
    rolling_source_window,
    schedule_source_pacing_points,
    source_pacing_plan_hash,
    wall_datetime,
)
from ..source_capacity_plans import apply_source_capacity_plan
from ..source_owner_cursor import attach_owner_history, pacing_source_key_hash
from .channel_like_expiration import active_like_messages, close_expired_like_obligations
from .channel_like_capability import reaction_capability_revision
from .channel_like_planning import like_actions_for_messages
from .channel_like_reactions import reaction_plan as _reaction_plan
from .channel_like_types import LikePlanItem, LikePlanningSpec
from .channel_like_album import logical_like_messages
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
    channel, messages = channel_scope(session, task, config)
    if not channel or not messages:
        return 0
    now_value = _now()
    ledger = ensure_task_day_ledger(session, task, now=now_value)
    close_expired_like_obligations(session, task, now_value=now_value)
    messages = active_like_messages(task, messages, now_value=now_value)
    if not messages:
        task.last_error = ""
        return 0
    return _build_like_actions(
        session,
        task,
        channel=channel,
        messages=messages,
        config=config,
        ledger=ledger,
        now_value=now_value,
    )


def _build_like_actions(
    session: Session,
    task: Task,
    *,
    channel: OperationTarget,
    messages: list[ChannelMessage],
    config: dict,
    ledger,
    now_value: datetime,
) -> int:
    prepared = _prepare_like_spec(
        session,
        task,
        channel=channel,
        messages=messages,
        config=config,
        ledger=ledger,
        now_value=now_value,
    )
    if prepared is None:
        return 0
    spec, target_per_message, account_ids_by_message = prepared
    actions = like_actions_for_messages(session, task, spec)
    if not actions:
        task.last_error = _empty_like_plan_message(
            session, task, messages=messages, target_per_message=target_per_message,
            account_ids_by_message=account_ids_by_message,
        )
        return 0
    return _create_like_actions(
        session, task, channel=channel, config=config, actions=actions
    )


def _prepare_like_spec(
    session: Session,
    task: Task,
    *,
    channel: OperationTarget,
    messages: list[ChannelMessage],
    config: dict,
    ledger,
    now_value: datetime,
) -> tuple[LikePlanningSpec, int, dict[int, set[int]]] | None:
    reactions = config.get("allowed_reactions") or list(
        DEFAULT_CHANNEL_LIKE_ALLOWED_REACTIONS
    )
    target_per_message = int(config.get("target_likes_per_message") or 1)
    account_ids_by_message = reaction_account_ids_for_messages(
        session,
        task,
        messages,
    )
    epoch = ensure_reaction_capacity_epoch(
        session,
        task,
        ledger,
        messages=logical_like_messages(messages) if config.get("engagement_contract_version") == "unified_engagement_v1" else messages,
        target=channel,
    )
    frozen_allocations = (
        allocated_account_ids_by_message(epoch) if epoch is not None else None
    )
    required_ids = (
        {
            int(account_id)
            for ids in frozen_allocations.values()
            for account_id in ids
        }
        if frozen_allocations is not None
        else None
    )
    admissible_ids = (
        reaction_admissible_account_ids(session, epoch) if epoch is not None else None
    )
    accounts = _like_accounts(
        session,
        task,
        channel=channel,
        config=config,
        target=target_per_message,
        required_ids=required_ids,
        admissible_ids=admissible_ids,
    )
    if not accounts:
        task.last_error = "没有可用账号，等待账号恢复后继续执行"
        return None
    record_channel_capacity_warning(task, "点赞", target_per_message, len(accounts))
    return (
        LikePlanningSpec(
            config=config,
            messages=messages,
            accounts=accounts,
            reactions=reactions,
            target_per_message=target_per_message,
            account_ids_by_message=account_ids_by_message,
            allocated_ids_by_message=frozen_allocations,
            now=now_value,
        ),
        target_per_message,
        account_ids_by_message,
    )


def _like_accounts(
    session: Session,
    task: Task,
    *,
    channel: OperationTarget,
    config: dict,
    target: int,
    required_ids: set[int] | None,
    admissible_ids: set[int] | None,
) -> list:
    if required_ids is not None:
        rows = session.scalars(select(TgAccount).where(
            TgAccount.tenant_id == task.tenant_id,
            TgAccount.id.in_(required_ids),
        ))
        by_id = {row.id: row for row in rows}
        candidates = [
            by_id[account_id]
            for account_id in sorted(required_ids)
            if account_id in by_id
            and (admissible_ids is None or account_id in admissible_ids)
        ]
        return channel_member_accounts(session, task, channel, candidates)
    _lower, maximum = quantity_jitter_bounds(
        target,
        float(config.get("like_count_jitter") or 0),
    )
    configured = int((task.account_config or {}).get("max_concurrent") or maximum)
    candidates = select_task_accounts(
        session,
        task.tenant_id,
        task.account_config or {},
        limit=max(maximum, configured),
        enforce_max_concurrent=False,
        daily_coverage_task_id=task.id,
        daily_coverage_action_types=("like_message",),
    )
    return channel_member_accounts(session, task, channel, candidates)


def _create_like_actions(
    session: Session,
    task: Task,
    *,
    channel: OperationTarget,
    config: dict,
    actions: list[LikePlanItem],
) -> int:
    now_value = _now()
    owners = {
        _like_slot_key(task, item): frozen_reaction_obligation(session, task, item=item)
        if item.obligation_id else ensure_reaction_obligation(
            session, task, item.message, item.account_id,
        )
        for item in actions
    }
    points_by_slot = _like_due_by_slot(
        session,
        task,
        channel=channel,
        actions=actions,
        owners=owners,
        now_at=now_value,
    )
    created = 0
    if task.fulfillment_contract_version == CURRENT_CONTRACT_VERSION and len(points_by_slot) < len(actions):
        _record_like_shortfall(task, len(actions), len(points_by_slot))
    for item in actions:
        slot_key = _like_slot_key(task, item)
        point = points_by_slot.get(slot_key)
        if point is None:
            continue
        due_at = point.due_at if hasattr(point, "due_at") else point
        release_at = (
            point.release_not_before_at if hasattr(point, "release_not_before_at")
            else due_at
        )
        created += _create_one_like_action(
            session, task, channel=channel, config=config, item=item,
            obligation=owners[slot_key], due_at=due_at, release_at=release_at,
        )
    return created


def _like_due_by_slot(
    session: Session,
    task: Task,
    *,
    channel: OperationTarget,
    actions: list[LikePlanItem],
    owners: dict[str, object],
    now_at,
) -> dict[str, object]:
    if task.fulfillment_contract_version == CURRENT_CONTRACT_VERSION:
        source_hash = pacing_source_key_hash(channel.tg_peer_id)
        slots = [
            _like_source_slot(
                task,
                item,
                owner=owners[_like_slot_key(task, item)],
                source_hash=source_hash,
            )
            for item in actions
        ]
        slots = attach_owner_history(
            session,
            task,
            slots,
            owner_model=ReactionFulfillmentObligation,
            config=task.pacing_config or {},
            seed_id=f"like:{task.id}",
            allow_plan_total_overrun=True,
        )
        points = schedule_source_pacing_points(
            slots,
            task.pacing_config or {},
            now_at=wall_datetime(now_at),
            timezone_name=task.timezone,
            seed_id=f"like:{task.id}",
        )
        points, slots = apply_source_capacity_plan(
            session,
            task,
            slots,
            points=points,
            pacing_domain="reaction",
        )
        for slot in slots:
            owner = owners[slot.slot_key]
            owner.source_capacity_plan_hash = slot.source_capacity_plan_hash
            owner.source_capacity_slot_ordinal = slot.source_capacity_slot_ordinal
        return points
    deadline = next_local_day_deadline(now_at, task.timezone)
    times = schedule_times(
        len(actions), task.pacing_config or {}, start_at=now_at,
        deadline_at=deadline, preserve_minimum_spacing=True,
    )
    times = reserve_task_schedule_times(
        session, task, "like_message", times,
        pacing_config=task.pacing_config or {}, deadline_at=deadline,
    )
    return {
        _like_slot_key(task, item): due_at
        for item, due_at in zip(actions, times, strict=False)
    }


def _create_one_like_action(
    session: Session,
    task: Task,
    *,
    channel: OperationTarget,
    config: dict,
    item: LikePlanItem,
    obligation,
    due_at,
    release_at,
) -> int:
    if not obligation_accepts_new_action(obligation):
        return 0
    planned_at = adjust_for_account_hour_limit(
        session, task, item.account_id, "like_message", due_at, config,
    )
    schedule = _like_account_schedule(
        session, task, channel=channel, item=item, obligation=obligation,
        due_at=due_at, release_at=release_at, planned_at=planned_at,
    )
    if schedule is None:
        return 0
    planned_at, reservation = schedule
    source_revision = _like_source_revision(session, item.message)
    payload = LikeMessagePayload(
        **channel_message_payload(channel, item.message),
        reaction_emoji=item.reaction,
        reaction_contract_version=obligation.reaction_contract_version,
        reaction_fulfillment_obligation_id=obligation.id,
        reaction_source_content_hash=(
            source_revision.source_content_hash if source_revision else ""
        ),
        reaction_capability_revision=reaction_capability_revision(channel),
    )
    action = create_like_action(session, task, item.account_id, planned_at, payload)
    if task.fulfillment_contract_version == CURRENT_CONTRACT_VERSION:
        freeze_action_pacing(action, obligation, slot_key=_like_slot_key(task, item))
        bind_account_pacing_reservation(reservation, action)
    bind_obligation_action(obligation, action)
    return 1


def _like_source_revision(
    session: Session,
    message: ChannelMessage,
) -> ChannelMessageSourceRevision | None:
    if not message.current_source_revision_id:
        return None
    revision = session.get(
        ChannelMessageSourceRevision, str(message.current_source_revision_id)
    )
    if revision is None or revision.channel_message_id != message.id:
        return None
    return revision


def _like_account_schedule(
    session: Session,
    task: Task,
    *,
    channel: OperationTarget,
    item: LikePlanItem,
    obligation,
    due_at,
    release_at,
    planned_at,
):
    if task.fulfillment_contract_version != CURRENT_CONTRACT_VERSION:
        return planned_at, None
    source = _like_source_slot(
        task,
        item,
        owner=obligation,
        source_hash=pacing_source_key_hash(channel.tg_peer_id),
    )
    freeze_pacing_owner(
        obligation,
        plan_hash=source_pacing_plan_hash(
            source, task.pacing_config or {}, seed_id=f"like:{task.id}",
        ),
        slot_ordinal=source.slot_ordinal,
        plan_total=source.plan_total,
        due_at=due_at,
        release_not_before_at=release_at,
        source_identity=source.owner_identity,
    )
    try:
        reservation = reserve_account_pacing(
            session, tenant_id=task.tenant_id, task_id=task.id,
            account_id=item.account_id, slot_key=_like_slot_key(task, item),
            due_at=due_at,
            release_not_before_at=latest_wall_datetime(release_at, planned_at),
            deadline_at=source.deadline_at,
            engagement_contract_version=str(
                (task.type_config or {}).get("engagement_contract_version") or ""
            ),
            action_class="reaction",
        )
    except AccountPacingDeadlineExceeded:
        _record_like_shortfall(task, 1, 0)
        return None
    return reservation.effective_claim_at, reservation


def _record_like_shortfall(task: Task, requested: int, scheduled: int) -> None:
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
        task.last_error = "来源滚动窗口内无合法节奏窗口可安排点赞义务，形成 pacing shortfall"


def _like_slot_key(task: Task, item: LikePlanItem) -> str:
    return f"like:{task.id}:{item.message.id}:{item.account_id}"


def _like_source_slot(
    task: Task,
    item: LikePlanItem,
    *,
    owner: ReactionFulfillmentObligation,
    source_hash: str,
) -> SourcePacingSlot:
    period_start, deadline = rolling_source_window(task, item.message.created_at)
    pacing_ordinal = (
        int(owner.pacing_slot_ordinal)
        if owner.pacing_slot_ordinal is not None
        else item.slot_ordinal
    )
    return SourcePacingSlot(
        source_key=str(item.message.id),
        slot_key=_like_slot_key(task, item),
        slot_ordinal=pacing_ordinal,
        plan_total=(
            int(owner.pacing_plan_total)
            if owner.pacing_due_at is not None and owner.pacing_plan_total
            else item.plan_total
        ),
        period_start_at=period_start,
        deadline_at=deadline,
        release_not_before_at=owner.release_not_before_at,
        frozen_due_at=owner.pacing_due_at,
        owner_id=owner.id,
        task_lifecycle_epoch=int(task.task_lifecycle_epoch or 1),
        pacing_period_key=f"message:{item.message.id}",
        pacing_source_key_hash=source_hash,
        source_capacity_plan_hash=owner.source_capacity_plan_hash,
        source_capacity_slot_ordinal=owner.source_capacity_slot_ordinal,
    )


def _empty_like_plan_message(
    session: Session,
    task: Task,
    *,
    messages: list[ChannelMessage],
    target_per_message: int,
    account_ids_by_message: dict[int, set[int]],
) -> str:
    from ..album_reaction_facts import album_targets_confirmed

    albums = {m.grouped_id for m in messages if m.grouped_id}
    ordinary = [m for m in messages if not m.grouped_id]
    if albums and (task.type_config or {}).get("engagement_contract_version") == "unified_engagement_v1":
        albums_done = album_targets_confirmed(session, task, album_ids=albums)
        ordinary_done = not ordinary or _all_like_targets_reached(ordinary, target_per_message, account_ids_by_message)
        if albums_done and ordinary_done:
            return ""
    unavailable_ids = {
        int(value)
        for value in (task.stats or {}).get(
            "reaction_capability_unavailable_message_ids",
            [],
        )
    }
    if any(message.id in unavailable_ids for message in messages):
        return task.last_error or "频道消息当前不可点赞"
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


__all__ = ["build_plan"]

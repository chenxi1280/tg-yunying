from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    ContentMixContract,
    ContentMixCycle,
    ContentMixCycleSlot,
    ContentMixObligation,
)
from app.models.enums import now


FINAL_SLOT_STATES = frozenset({"confirmed", "terminal"})
MATERIALIZED_SLOT_STATES = frozenset(
    {
        "pending",
        "gateway_started",
        "unknown",
        "confirmed",
        "replan_required",
        "terminal",
    }
)


@dataclass(frozen=True)
class ContentMixSlotSpec:
    primary_quantity_slot_id: str
    relation_kind: str
    reply_requirement_key: str = ""
    initial_reply_to_message_id: str = ""


@dataclass(frozen=True)
class ContentMixCycleSpec:
    tenant_id: int
    task_id: str
    target_operation_target_id: int
    task_day_ledger_id: str
    cycle_seq: int
    config_revision: int
    allocation_seed: str
    slots: tuple[ContentMixSlotSpec, ...]
    reply_min_required_count: int = 0
    normal_text_emoji_required_count: int = 0
    normal_text_emoji_max_count: int = 0
    image_required_count: int = 0
    image_max_count: int = 0
    sticker_required_count: int = 0
    sticker_max_count: int = 0
    custom_emoji_required_count: int = 0
    custom_emoji_max_count: int = 0


def _validate_spec(spec: ContentMixCycleSpec) -> None:
    slot_count = len(spec.slots)
    quantity_ids = {item.primary_quantity_slot_id for item in spec.slots}
    if slot_count == 0 or len(quantity_ids) != slot_count:
        raise ValueError("content_mix_policy_invalid")
    if any(item.relation_kind not in {"direct", "reply"} for item in spec.slots):
        raise ValueError("content_mix_policy_invalid")
    reply_count = sum(item.relation_kind == "reply" for item in spec.slots)
    if spec.reply_min_required_count > reply_count:
        raise ValueError("content_mix_policy_invalid")
    for required, maximum in _material_limits(spec):
        if required < 0 or maximum < required or maximum > slot_count:
            raise ValueError("content_mix_policy_invalid")


def _material_limits(spec: ContentMixCycleSpec) -> tuple[tuple[int, int], ...]:
    return (
        (
            spec.normal_text_emoji_required_count,
            spec.normal_text_emoji_max_count,
        ),
        (spec.image_required_count, spec.image_max_count),
        (spec.sticker_required_count, spec.sticker_max_count),
        (spec.custom_emoji_required_count, spec.custom_emoji_max_count),
    )


def _new_cycle(spec: ContentMixCycleSpec, closed_at: datetime) -> ContentMixCycle:
    return ContentMixCycle(
        tenant_id=spec.tenant_id,
        task_id=spec.task_id,
        target_operation_target_id=spec.target_operation_target_id,
        task_day_ledger_id=spec.task_day_ledger_id,
        cycle_seq=spec.cycle_seq,
        config_revision=spec.config_revision,
        scope_total_slots=len(spec.slots),
        allocation_seed=spec.allocation_seed,
        allocation_closed_at=closed_at,
    )


def _new_contract(
    spec: ContentMixCycleSpec,
    cycle: ContentMixCycle,
) -> ContentMixContract:
    reply_count = sum(item.relation_kind == "reply" for item in spec.slots)
    return ContentMixContract(
        tenant_id=spec.tenant_id,
        content_mix_scope_key=(
            f"ai:{spec.task_id}:{spec.target_operation_target_id}:"
            f"{cycle.id}:{spec.config_revision}"
        ),
        content_contract_version=spec.config_revision,
        scope_total_slots=len(spec.slots),
        allocation_seed=spec.allocation_seed,
        reply_min_required_count=spec.reply_min_required_count,
        reply_planned_count=reply_count,
        direct_planned_count=len(spec.slots) - reply_count,
        normal_text_emoji_required_count=spec.normal_text_emoji_required_count,
        normal_text_emoji_max_count=spec.normal_text_emoji_max_count,
        image_required_count=spec.image_required_count,
        image_max_count=spec.image_max_count,
        sticker_required_count=spec.sticker_required_count,
        sticker_max_count=spec.sticker_max_count,
        custom_emoji_required_count=spec.custom_emoji_required_count,
        custom_emoji_max_count=spec.custom_emoji_max_count,
    )


def _add_slots(
    session: Session,
    spec: ContentMixCycleSpec,
    cycle: ContentMixCycle,
) -> None:
    session.add_all(
        [
            ContentMixCycleSlot(
                tenant_id=spec.tenant_id,
                cycle_id=cycle.id,
                slot_index=index,
                primary_quantity_slot_id=item.primary_quantity_slot_id,
                relation_kind=item.relation_kind,
                reply_requirement_key=item.reply_requirement_key,
                initial_reply_to_message_id=item.initial_reply_to_message_id,
            )
            for index, item in enumerate(spec.slots, start=1)
        ]
    )


def _policy_minimums(spec: ContentMixCycleSpec) -> tuple[tuple[str, int], ...]:
    return tuple(
        (kind, count)
        for kind, count in (
            ("reply", spec.reply_min_required_count),
            ("normal_text_emoji", spec.normal_text_emoji_required_count),
            ("image", spec.image_required_count),
            ("sticker", spec.sticker_required_count),
            ("custom_emoji", spec.custom_emoji_required_count),
        )
        if count > 0
    )


def _add_obligations(
    session: Session,
    spec: ContentMixCycleSpec,
    contract: ContentMixContract,
) -> None:
    session.add_all(
        [
            ContentMixObligation(
                tenant_id=spec.tenant_id,
                content_mix_contract_id=contract.id,
                obligation_source="policy_min",
                obligation_kind=kind,
                required_count=count,
                planned_count=count,
            )
            for kind, count in _policy_minimums(spec)
        ]
    )


def create_content_mix_cycle(
    session: Session,
    spec: ContentMixCycleSpec,
) -> ContentMixCycle:
    _validate_spec(spec)
    with session.begin_nested():
        cycle = _new_cycle(spec, now())
        session.add(cycle)
        session.flush()
        contract = _new_contract(spec, cycle)
        session.add(contract)
        session.flush()
        _add_slots(session, spec, cycle)
        _add_obligations(session, spec, contract)
        session.flush()
    return cycle


def mark_cycle_slot_materialized(
    session: Session,
    slot: ContentMixCycleSlot,
    *,
    action_id: str | None,
) -> None:
    if slot.slot_state == "unmaterialized":
        slot.slot_state = "pending"
        slot.slot_attempt = max(slot.slot_attempt, 1)
        slot.current_action_id = action_id
    session.flush()
    cycle = session.get(ContentMixCycle, slot.cycle_id)
    if cycle is None:
        raise ValueError("content_mix_cycle_not_found")
    count = session.scalar(
        select(func.count(ContentMixCycleSlot.id)).where(
            ContentMixCycleSlot.cycle_id == cycle.id,
            ContentMixCycleSlot.slot_state.in_(MATERIALIZED_SLOT_STATES),
        )
    )
    cycle.materialized_slot_count = int(count or 0)
    if cycle.materialized_slot_count == cycle.scope_total_slots:
        cycle.materialization_status = "complete"
    elif cycle.materialized_slot_count > 0:
        cycle.materialization_status = "partial"


def _contract_for_cycle(
    session: Session,
    cycle: ContentMixCycle,
) -> ContentMixContract:
    scope_key = (
        f"ai:{cycle.task_id}:{cycle.target_operation_target_id}:"
        f"{cycle.id}:{cycle.config_revision}"
    )
    contract = session.scalar(
        select(ContentMixContract).where(
            ContentMixContract.content_mix_scope_key == scope_key
        )
    )
    if contract is None:
        raise ValueError("content_mix_contract_unreplayable")
    return contract


def reconcile_content_mix_cycle(
    session: Session,
    cycle: ContentMixCycle,
    *,
    observed_at: datetime | None = None,
) -> bool:
    if cycle.settlement_status == "settled":
        return True
    slots = session.scalars(
        select(ContentMixCycleSlot).where(ContentMixCycleSlot.cycle_id == cycle.id)
    ).all()
    if len(slots) != cycle.scope_total_slots:
        return False
    if any(slot.slot_state not in FINAL_SLOT_STATES for slot in slots):
        return False
    contract = _contract_for_cycle(session, cycle)
    obligations = session.scalars(
        select(ContentMixObligation).where(
            ContentMixObligation.content_mix_contract_id == contract.id
        )
    ).all()
    if any(item.status not in {"met", "shortfall"} for item in obligations):
        return False
    settled_at = observed_at or now()
    has_shortfall = any(slot.slot_state == "terminal" for slot in slots)
    has_shortfall = has_shortfall or any(
        item.status == "shortfall" for item in obligations
    )
    cycle.settlement_status = "settled"
    cycle.settled_at = settled_at
    if has_shortfall and _utc(settled_at) > _utc(_cycle_deadline(session, cycle)):
        cycle.settlement_outcome = "missed"
    elif has_shortfall:
        cycle.settlement_outcome = "shortfall"
    else:
        cycle.settlement_outcome = "met"
    return True


def _cycle_deadline(session: Session, cycle: ContentMixCycle) -> datetime:
    from app.models import TaskDayLedger

    ledger = session.get(TaskDayLedger, cycle.task_day_ledger_id)
    if ledger is None:
        raise ValueError("task_day_ledger_not_found")
    return ledger.deadline_at


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


__all__ = [
    "ContentMixCycleSpec",
    "ContentMixSlotSpec",
    "create_content_mix_cycle",
    "mark_cycle_slot_materialized",
    "reconcile_content_mix_cycle",
]

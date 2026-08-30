from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from datetime import datetime
import hashlib

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    CommentFulfillmentObligation,
    ReactionFulfillmentObligation,
    Task,
    TaskDayLedger,
    TaskGroupDailyMessageSlot,
    ViewFulfillmentObligation,
)

from .source_pacing import SourcePacingSlot, wall_datetime
from .pacing_persistence import PacingOwnerImmutableConflict


PACING_OWNER_MODELS = {
    model.__tablename__: model
    for model in (
        CommentFulfillmentObligation,
        ReactionFulfillmentObligation,
        TaskGroupDailyMessageSlot,
        ViewFulfillmentObligation,
    )
}


def pacing_source_key_hash(peer: str) -> str:
    normalized = str(peer or "").strip().lower()
    if not normalized:
        raise ValueError("pacing_source_identity_incomplete")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def advance_owner_release(
    session: Session,
    *,
    owner_type: str,
    owner_id: str,
    not_before: datetime,
) -> None:
    model = PACING_OWNER_MODELS.get(owner_type)
    if model is None:
        raise ValueError("pacing_source_owner_type_invalid")
    owner = session.get(model, owner_id)
    if owner is None:
        raise LookupError("pacing_source_owner_missing")
    current = getattr(owner, "release_not_before_at", None)
    owner.release_not_before_at = max(
        wall_datetime(current),
        not_before,
    ) if current is not None else not_before


def attach_owner_history(
    session: Session,
    task: Task,
    slots: list[SourcePacingSlot],
    *,
    owner_model,
    config: dict,
    seed_id: str,
    allow_plan_total_overrun: bool = False,
) -> list[SourcePacingSlot]:
    grouped: dict[tuple, list[SourcePacingSlot]] = defaultdict(list)
    for slot in slots:
        key = slot.owner_identity
        if not slot.owner_id or not all(slot.owner_identity[1:]):
            raise ValueError("pacing_source_identity_incomplete")
        grouped[key].append(slot)
    enriched: dict[str, SourcePacingSlot] = {}
    for key, group in grouped.items():
        cursor, ordinal = _owner_history(
            session,
            task,
            owner_model=owner_model,
            lifecycle_epoch=key[0],
            period_key=key[1],
            source_hash=key[2],
            excluded_owner_ids=[slot.owner_id for slot in group],
        )
        ordinal = _include_frozen_group_ordinal(group, ordinal)
        allocated = _allocate_new_ordinals(
            group,
            ordinal,
            allow_plan_total_overrun=allow_plan_total_overrun,
        )
        for slot in allocated:
            enriched[slot.slot_key] = replace(
                slot,
                historical_cursor_at=cursor,
                historical_max_ordinal=ordinal,
            )
    return [enriched[slot.slot_key] for slot in slots]


def _include_frozen_group_ordinal(
    slots: list[SourcePacingSlot],
    ordinal: int | None,
) -> int | None:
    frozen = [slot for slot in slots if slot.frozen_due_at is not None]
    ordinals = [slot.slot_ordinal for slot in frozen]
    return max([ordinal, *ordinals]) if ordinal is not None else max(ordinals, default=None)


def _allocate_new_ordinals(
    slots: list[SourcePacingSlot],
    historical_max_ordinal: int | None,
    *,
    allow_plan_total_overrun: bool,
) -> list[SourcePacingSlot]:
    pending = sorted(
        (slot for slot in slots if slot.frozen_due_at is None),
        key=lambda slot: (slot.slot_ordinal, slot.slot_key),
    )
    if not pending:
        return slots
    next_ordinal = 0 if historical_max_ordinal is None else historical_max_ordinal + 1
    expanded_total = max(
        [slot.plan_total for slot in pending]
        + ([next_ordinal + len(pending)] if allow_plan_total_overrun else [])
    )
    replacements: dict[str, SourcePacingSlot] = {}
    for slot in pending:
        if next_ordinal >= slot.plan_total and not allow_plan_total_overrun:
            raise PacingOwnerImmutableConflict("pacing_source_plan_exhausted")
        replacements[slot.slot_key] = replace(
            slot,
            slot_ordinal=next_ordinal,
            plan_total=expanded_total,
        )
        next_ordinal += 1
    return [replacements.get(slot.slot_key, slot) for slot in slots]


def _owner_history(
    session: Session,
    task: Task,
    *,
    owner_model,
    lifecycle_epoch: int,
    period_key: str,
    source_hash: str,
    excluded_owner_ids: list[str],
):
    _lock_source_cursor(
        session,
        tenant_id=task.tenant_id,
        owner_type=owner_model.__tablename__,
        period_key=period_key,
        source_hash=source_hash,
    )
    statement = select(
        func.max(owner_model.release_not_before_at),
        func.max(owner_model.pacing_slot_ordinal),
    ).select_from(owner_model)
    filters = [
        owner_model.tenant_id == task.tenant_id,
        owner_model.task_lifecycle_epoch == lifecycle_epoch,
        owner_model.pacing_period_key == period_key,
        owner_model.pacing_source_key_hash == source_hash,
        owner_model.release_not_before_at.is_not(None),
        owner_model.id.not_in(excluded_owner_ids),
    ]
    if owner_model is ViewFulfillmentObligation:
        statement = statement.join(
            TaskDayLedger,
            TaskDayLedger.id == ViewFulfillmentObligation.task_day_ledger_id,
        )
        filters.append(TaskDayLedger.task_id == task.id)
    else:
        filters.append(owner_model.task_id == task.id)
    cursor, ordinal = session.execute(statement.where(*filters)).one()
    return cursor, int(ordinal) if ordinal is not None else None


def _lock_source_cursor(
    session: Session,
    *,
    tenant_id: int,
    owner_type: str,
    period_key: str,
    source_hash: str,
) -> None:
    if session.get_bind().dialect.name != "postgresql":
        return
    raw = f"{tenant_id}:{owner_type}:{period_key}:{source_hash}"
    key = int.from_bytes(hashlib.sha256(raw.encode("utf-8")).digest()[:8], "big")
    session.execute(select(func.pg_advisory_xact_lock(key & ((1 << 63) - 1))))


__all__ = ["advance_owner_release", "attach_owner_history", "pacing_source_key_hash"]

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

from .source_pacing import SourcePacingSlot, source_pacing_plan_hash, wall_datetime


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
) -> list[SourcePacingSlot]:
    grouped: dict[tuple, list[SourcePacingSlot]] = defaultdict(list)
    for slot in slots:
        plan_hash = source_pacing_plan_hash(slot, config, seed_id=seed_id)
        key = (*slot.owner_identity, plan_hash)
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
            plan_hash=key[3],
            excluded_owner_ids=[slot.owner_id for slot in group],
        )
        for slot in group:
            enriched[slot.slot_key] = replace(
                slot,
                historical_cursor_at=cursor,
                historical_max_ordinal=ordinal,
            )
    return [enriched[slot.slot_key] for slot in slots]


def _owner_history(
    session: Session,
    task: Task,
    *,
    owner_model,
    lifecycle_epoch: int,
    period_key: str,
    source_hash: str,
    plan_hash: str,
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
        owner_model.pacing_plan_hash == plan_hash,
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

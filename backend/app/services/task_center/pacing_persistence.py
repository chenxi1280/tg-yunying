from __future__ import annotations

from datetime import datetime

from app.models import Action
from app.timezone import BEIJING_TZ

from .pacing import PACING_CONTRACT_VERSION


def freeze_pacing_owner(
    owner,
    *,
    plan_hash: str,
    slot_ordinal: int,
    plan_total: int,
    due_at: datetime,
    release_not_before_at: datetime | None = None,
) -> datetime:
    existing = getattr(owner, "pacing_due_at", None)
    if existing is not None:
        _assert_frozen_identity(owner, plan_hash, slot_ordinal, plan_total, due_at)
        return _freeze_owner_release(owner, due_at, release_not_before_at)
    owner.pacing_contract_version = PACING_CONTRACT_VERSION
    owner.pacing_plan_hash = plan_hash
    owner.pacing_slot_ordinal = slot_ordinal
    if hasattr(owner, "pacing_plan_total"):
        owner.pacing_plan_total = plan_total
    owner.pacing_due_at = due_at
    owner.release_not_before_at = release_not_before_at or due_at
    return owner.release_not_before_at


def _freeze_owner_release(
    owner,
    due_at: datetime,
    proposed: datetime | None,
) -> datetime:
    current = getattr(owner, "release_not_before_at", None)
    if current is None or (_same_wall_time(current, due_at) and proposed is not None):
        owner.release_not_before_at = proposed or due_at
    return owner.release_not_before_at


def freeze_action_pacing(
    action: Action,
    owner,
    *,
    slot_key: str,
) -> None:
    action.pacing_contract_version = owner.pacing_contract_version
    action.pacing_plan_hash = owner.pacing_plan_hash
    action.pacing_slot_key = slot_key
    action.pacing_slot_ordinal = owner.pacing_slot_ordinal
    action.pacing_due_at = owner.pacing_due_at
    action.release_not_before_at = owner.release_not_before_at
    action.effective_claim_at = action.scheduled_at


def _assert_frozen_identity(
    owner,
    plan_hash: str,
    slot_ordinal: int,
    plan_total: int,
    due_at: datetime,
) -> None:
    current_total = getattr(owner, "pacing_plan_total", plan_total)
    values = (
        owner.pacing_plan_hash == plan_hash,
        owner.pacing_slot_ordinal == slot_ordinal,
        current_total == plan_total,
        _same_wall_time(owner.pacing_due_at, due_at),
    )
    if not all(values):
        raise ValueError("pacing_owner_immutable_conflict")


def _same_wall_time(left: datetime, right: datetime) -> bool:
    return _beijing_wall_time(left) == _beijing_wall_time(right)


def _beijing_wall_time(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(BEIJING_TZ).replace(tzinfo=None)


__all__ = ["freeze_action_pacing", "freeze_pacing_owner"]

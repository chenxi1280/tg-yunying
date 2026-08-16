from __future__ import annotations

from datetime import datetime

from app.models import Action
from app.timezone import BEIJING_TZ

from .pacing import PACING_CONTRACT_VERSION


class PacingOwnerImmutableConflict(ValueError):
    """pacing 冻结身份发生不可逆变化且不满足单调上调迁移条件。

    2026-08-16 生产事故（PRD §2.4）：目标上调后旧冻结 slot 的 plan_total 与新计划
    不一致，该确定性冲突必须由 planner 按 typed blocker 处理，不得进入通用 30 秒重试。
    """


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
        if _assert_frozen_identity(owner, plan_hash, slot_ordinal, plan_total, due_at):
            # 目标上调迁移：identity 不变、仅 plan_total 单调上调时，允许未绑定
            # active Action 的 owner 升级冻结的 total/due/release（quantity_ordinal、
            # due_unit_key 与 immutable settlement 合同不受影响）。
            if hasattr(owner, "pacing_plan_total"):
                owner.pacing_plan_total = plan_total
            owner.pacing_due_at = due_at
            owner.release_not_before_at = release_not_before_at or due_at
            return owner.release_not_before_at
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
) -> bool:
    """校验已冻结 owner 与新计划一致；返回 True 表示允许目标单调上调迁移。"""
    current_total = (
        owner.pacing_plan_total if hasattr(owner, "pacing_plan_total") else plan_total
    )
    identity_match = (
        owner.pacing_plan_hash == plan_hash,
        owner.pacing_slot_ordinal == slot_ordinal,
    )
    if (
        all(identity_match)
        and current_total is not None
        and plan_total > int(current_total)
    ):
        return True
    values = (
        *identity_match,
        current_total == plan_total,
        _same_wall_time(owner.pacing_due_at, due_at),
    )
    if not all(values):
        raise PacingOwnerImmutableConflict("pacing_owner_immutable_conflict")
    return False


def _same_wall_time(left: datetime, right: datetime) -> bool:
    return _beijing_wall_time(left) == _beijing_wall_time(right)


def _beijing_wall_time(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(BEIJING_TZ).replace(tzinfo=None)


__all__ = ["PacingOwnerImmutableConflict", "freeze_action_pacing", "freeze_pacing_owner"]

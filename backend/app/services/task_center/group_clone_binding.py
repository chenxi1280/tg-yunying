from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Task, TgAccountAuthorization
from app.models.group_clone import (
    CloneAccountSlot,
    CloneDeliveryObligation,
    CloneSenderBindingHistory,
)

ACTIVE_BINDING_STATES = ("active", "guarded", "eligible")
OPEN_OBLIGATION_STATES = (
    "observed",
    "waiting_source_base",
    "waiting_binding",
    "waiting_album",
    "waiting_dependency",
    "waiting_transport",
    "waiting_manual_review",
    "ready",
    "action_bound",
    "executing",
    "unknown_after_send",
    "remote_reconcile_only",
)


class CloneSenderBindingManager:
    @staticmethod
    def get_or_assign_sender_binding(
        session: Session,
        task: Task,
        *,
        source_sender_peer_type: str,
        source_sender_peer_id: str,
        source_sender_name: str,
        reply_to_sender_peer_id: Optional[str] = None,
        is_vip: bool = False,
    ) -> Tuple[Optional[CloneSenderBindingHistory], str]:
        existing = _active_binding(
            session,
            task,
            peer_type=source_sender_peer_type,
            peer_id=source_sender_peer_id,
        )
        if existing:
            return _reuse_binding(
                session, task, existing=existing,
                parent_peer_id=reply_to_sender_peer_id, is_vip=is_vip,
            )
        parent_account_id = _parent_account_id(session, task, reply_to_sender_peer_id)
        slot, reclaimed = _claim_slot(session, task, parent_account_id)
        if slot is None:
            return None, "sender_pool_exhausted: 无可安全分配的持久账号槽位"
        if reclaimed:
            _expire_binding(reclaimed, source_sender_peer_id)
        binding = _new_binding(
            session,
            task,
            slot,
            peer_type=source_sender_peer_type,
            peer_id=source_sender_peer_id,
            name=source_sender_name,
            is_vip=is_vip,
        )
        return binding, ""

    @staticmethod
    def release_or_rebind(
        session: Session,
        task: Task,
        *,
        binding_id: str,
        expected_binding_version: int,
        replacement_account_id: int | None,
        reason: str,
    ) -> CloneSenderBindingHistory | None:
        binding = session.scalar(select(CloneSenderBindingHistory).where(
            CloneSenderBindingHistory.id == binding_id,
            CloneSenderBindingHistory.task_id == task.id,
            CloneSenderBindingHistory.task_lifecycle_epoch == task.task_lifecycle_epoch,
        ).with_for_update())
        if binding is None or binding.status not in ACTIVE_BINDING_STATES:
            raise ValueError("sender binding 不存在或已释放")
        if binding.binding_version != expected_binding_version:
            raise ValueError("sender binding version 已变化")
        if not _no_open_obligations(session, binding):
            raise ValueError("sender binding 仍有未收口义务，不能换绑")
        old_slot = session.get(CloneAccountSlot, binding.account_slot_id, with_for_update=True)
        replacement = _replacement_slot(
            session, task, account_id=replacement_account_id, old_slot=old_slot,
        )
        binding.status = "expired"
        binding.valid_to = datetime.now(timezone.utc)
        binding.last_reassigned_at = binding.valid_to
        binding.reassignment_reason = reason[:100]
        if old_slot is not None:
            old_slot.state = "available"
            old_slot.version += 1
        session.flush()
        if replacement is None:
            return None
        replacement.state = "active"
        replacement.version += 1
        return _new_binding(
            session, task, replacement,
            peer_type=binding.source_sender_peer_type,
            peer_id=binding.source_sender_peer_id,
            name=binding.source_sender_name,
            is_vip=binding.is_vip,
        )


def _active_binding(session, task, *, peer_type, peer_id):
    return session.scalar(
        select(CloneSenderBindingHistory)
        .where(
            CloneSenderBindingHistory.task_id == task.id,
            CloneSenderBindingHistory.task_lifecycle_epoch == task.task_lifecycle_epoch,
            CloneSenderBindingHistory.source_sender_peer_type == peer_type,
            CloneSenderBindingHistory.source_sender_peer_id == peer_id,
            CloneSenderBindingHistory.status.in_(ACTIVE_BINDING_STATES),
        )
        .with_for_update()
    )


def _reuse_binding(session, task, *, existing, parent_peer_id, is_vip):
    parent_account_id = _parent_account_id(session, task, parent_peer_id)
    if (
        parent_peer_id
        and parent_peer_id != existing.source_sender_peer_id
        and parent_account_id == existing.assigned_account_id
    ):
        return None, "reply_self_collision: 回复父消息与当前发言人映射到同一账号"
    existing.last_spoken_at = datetime.now(timezone.utc)
    existing.status = "active"
    existing.is_vip = existing.is_vip or is_vip
    session.flush()
    return existing, ""


def _parent_account_id(session, task, parent_peer_id):
    if not parent_peer_id:
        return None
    return session.scalar(
        select(CloneSenderBindingHistory.assigned_account_id).where(
            CloneSenderBindingHistory.task_id == task.id,
            CloneSenderBindingHistory.task_lifecycle_epoch == task.task_lifecycle_epoch,
            CloneSenderBindingHistory.source_sender_peer_id == parent_peer_id,
            CloneSenderBindingHistory.status.in_(ACTIVE_BINDING_STATES),
        )
    )


def _claim_slot(session, task, excluded_account_id):
    now_value = datetime.now(timezone.utc)
    _advance_binding_states(session, task, now_value=now_value)
    account_ids = tuple((task.type_config or {}).get("sender_pool", {}).get("account_ids", ()))
    slots = session.scalars(
        select(CloneAccountSlot)
        .where(CloneAccountSlot.task_id == task.id, CloneAccountSlot.account_id.in_(account_ids))
        .order_by(CloneAccountSlot.account_id)
        .with_for_update()
    ).all()
    available = [slot for slot in slots if _slot_available(slot, excluded_account_id, now_value)]
    if available:
        return _activate_slot(available[0]), None
    reclaimable = _reclaimable_bindings(
        session, task, excluded_account_id=excluded_account_id, now_value=now_value,
    )
    if not reclaimable:
        return None, None
    binding = reclaimable[0]
    slot = next((item for item in slots if item.id == binding.account_slot_id), None)
    return (_activate_slot(slot), binding) if slot else (None, None)


def _slot_available(slot, excluded_account_id, now_value):
    if slot.account_id == excluded_account_id or slot.state != "available":
        return False
    blocked_until = slot.projected_transport_blocked_until
    if blocked_until is None:
        return True
    if blocked_until.tzinfo is None:
        blocked_until = blocked_until.replace(tzinfo=timezone.utc)
    return blocked_until <= now_value


def _activate_slot(slot):
    slot.state = "active"
    slot.version = int(slot.version or 1) + 1
    return slot


def _reclaimable_bindings(session, task, *, excluded_account_id, now_value):
    candidates = session.scalars(
        select(CloneSenderBindingHistory)
        .where(
            CloneSenderBindingHistory.task_id == task.id,
            CloneSenderBindingHistory.task_lifecycle_epoch == task.task_lifecycle_epoch,
            CloneSenderBindingHistory.status == "eligible",
            CloneSenderBindingHistory.is_vip.is_(False),
            CloneSenderBindingHistory.assigned_account_id != excluded_account_id,
        )
        .order_by(CloneSenderBindingHistory.last_spoken_at, CloneSenderBindingHistory.id)
        .with_for_update()
    ).all()
    return [
        item for item in candidates
        if _safe_to_reclaim(session, task, binding=item, now_value=now_value)
    ]


def _safe_to_reclaim(session, task, *, binding, now_value):
    pool = (task.type_config or {}).get("sender_pool", {})
    minimum_minutes = max(
        int(pool.get("minimum_tenure_minutes", 60)),
        int(pool.get("eligible_release_minutes", 720)),
    )
    valid_from = binding.valid_from
    if valid_from.tzinfo is None:
        valid_from = valid_from.replace(tzinfo=timezone.utc)
    if (now_value - valid_from).total_seconds() < minimum_minutes * 60:
        return False
    return _no_open_obligations(session, binding)


def _no_open_obligations(session, binding) -> bool:
    open_count = session.scalar(select(func.count()).select_from(CloneDeliveryObligation).where(
        CloneDeliveryObligation.binding_history_id == binding.id,
        CloneDeliveryObligation.state.in_(OPEN_OBLIGATION_STATES),
    ))
    return not open_count


def _replacement_slot(session, task, *, account_id, old_slot):
    if account_id is None:
        return None
    if old_slot is not None and old_slot.account_id == account_id:
        raise ValueError("replacement account 必须与当前账号不同")
    allowed = set((task.type_config or {}).get("sender_pool", {}).get("account_ids", ()))
    if account_id not in allowed:
        raise ValueError("replacement account 不在冻结 sender pool")
    slot = session.scalar(select(CloneAccountSlot).where(
        CloneAccountSlot.task_id == task.id,
        CloneAccountSlot.account_id == account_id,
    ).with_for_update())
    if slot is None or slot.state != "available":
        raise ValueError("replacement account slot 当前不可用")
    return slot


def _advance_binding_states(session, task, *, now_value) -> None:
    pool = (task.type_config or {}).get("sender_pool", {})
    active_minutes = int(pool.get("active_minutes", 30))
    guarded_minutes = int(pool.get("guarded_minutes", 120))
    bindings = session.scalars(select(CloneSenderBindingHistory).where(
        CloneSenderBindingHistory.task_id == task.id,
        CloneSenderBindingHistory.task_lifecycle_epoch == task.task_lifecycle_epoch,
        CloneSenderBindingHistory.status.in_(("active", "guarded")),
    ).with_for_update()).all()
    for binding in bindings:
        spoken_at = binding.last_spoken_at
        if spoken_at.tzinfo is None:
            spoken_at = spoken_at.replace(tzinfo=timezone.utc)
        age_minutes = (now_value - spoken_at).total_seconds() / 60
        if age_minutes < active_minutes:
            binding.status = "active"
        elif binding.is_vip or age_minutes < guarded_minutes:
            binding.status = "guarded"
        else:
            binding.status = "eligible"


def _expire_binding(binding, replacement_peer_id):
    now_value = datetime.now(timezone.utc)
    binding.status = "expired"
    binding.valid_to = now_value
    binding.reassignment_reason = f"safe_reassignment:{replacement_peer_id}"


def _new_binding(session, task, slot, *, peer_type, peer_id, name, is_vip):
    authorization = session.get(TgAccountAuthorization, slot.authorization_id)
    if authorization is None or not authorization.is_current or authorization.status != "active":
        raise RuntimeError("clone_account_slot_authorization_invalid")
    version = session.scalar(select(func.max(CloneSenderBindingHistory.binding_version)).where(
        CloneSenderBindingHistory.task_id == task.id,
        CloneSenderBindingHistory.task_lifecycle_epoch == task.task_lifecycle_epoch,
        CloneSenderBindingHistory.source_sender_peer_type == peer_type,
        CloneSenderBindingHistory.source_sender_peer_id == peer_id,
    )) or 0
    binding = CloneSenderBindingHistory(
        task_id=task.id,
        task_lifecycle_epoch=task.task_lifecycle_epoch,
        binding_version=version + 1,
        source_sender_peer_type=peer_type,
        source_sender_peer_id=peer_id,
        source_sender_name=name,
        assigned_account_id=slot.account_id,
        account_slot_id=slot.id,
        status="active",
        is_vip=is_vip,
    )
    session.add(binding)
    session.flush()
    return binding


__all__ = ["CloneSenderBindingManager"]

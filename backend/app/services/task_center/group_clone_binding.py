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
    if parent_account_id and parent_account_id == existing.assigned_account_id:
        return None, "reply_self_collision: 回复父消息与当前发言人映射到同一账号"
    existing.last_spoken_at = datetime.now(timezone.utc)
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
    account_ids = tuple((task.type_config or {}).get("sender_pool", {}).get("account_ids", ()))
    slots = session.scalars(
        select(CloneAccountSlot)
        .where(CloneAccountSlot.task_id == task.id, CloneAccountSlot.account_id.in_(account_ids))
        .order_by(CloneAccountSlot.account_id)
        .with_for_update()
    ).all()
    now_value = datetime.now(timezone.utc)
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
    minimum_minutes = int((task.type_config or {}).get("sender_pool", {}).get("minimum_tenure_minutes", 60))
    valid_from = binding.valid_from
    if valid_from.tzinfo is None:
        valid_from = valid_from.replace(tzinfo=timezone.utc)
    if (now_value - valid_from).total_seconds() < minimum_minutes * 60:
        return False
    open_count = session.scalar(select(func.count()).select_from(CloneDeliveryObligation).where(
        CloneDeliveryObligation.binding_history_id == binding.id,
        CloneDeliveryObligation.state.in_(OPEN_OBLIGATION_STATES),
    ))
    return not open_count


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

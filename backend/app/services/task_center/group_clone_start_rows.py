from __future__ import annotations

import hashlib

from app.models.group_clone import (
    CloneAccountSlot,
    CloneSourceStreamState,
    CloneTargetExecutionSnapshot,
    CloneTargetRouteSnapshot,
)
from app.models.telegram_updates import TelegramAuthorizationUpdateSubscription


def initialize_start_rows(session, task, *, payload, resolved, route_hash) -> None:
    session.add(CloneSourceStreamState(
        tenant_id=task.tenant_id,
        task_id=task.id,
        task_lifecycle_epoch=task.task_lifecycle_epoch,
        source_peer_type=payload.source.peer_type,
        source_peer_id=payload.source.peer_id,
        listener_account_id=payload.source.listener_account_id,
        authorization_id=payload.source.authorization_id,
        authorization_update_state_id=resolved.listener_update_state.id,
        last_consumed_ingress_order_no=resolved.listener_update_state.last_ingress_order_no,
        state="initializing",
    ))
    session.add(TelegramAuthorizationUpdateSubscription(
        authorization_update_state_id=resolved.listener_update_state.id,
        task_id=task.id,
        task_epoch=task.task_lifecycle_epoch,
        source_peer_type=payload.source.peer_type,
        source_peer_id=payload.source.peer_id,
        start_ingress_order=resolved.listener_update_state.last_ingress_order_no,
        state="initializing",
    ))
    route = _route_snapshot(task, payload, route_hash)
    session.add(route)
    session.flush()
    session.add(_control_snapshot(route, payload, resolved))
    for authorization in resolved.sender_authorizations:
        session.add(CloneAccountSlot(
            task_id=task.id,
            account_id=authorization.account_id,
            authorization_id=authorization.id,
        ))


def _route_snapshot(task, payload, route_hash) -> CloneTargetRouteSnapshot:
    return CloneTargetRouteSnapshot(
        tenant_id=task.tenant_id,
        task_id=task.id,
        epoch=task.task_lifecycle_epoch,
        config_revision=task.config_revision,
        source_internal_group_id=payload.source.internal_group_id,
        source_operation_target_id=str(payload.source.operation_target_id),
        source_peer_type=payload.source.peer_type,
        source_peer_id=payload.source.peer_id,
        target_internal_group_id=payload.target.internal_group_id,
        target_operation_target_id=str(payload.target.operation_target_id),
        target_peer_type=payload.target.peer_type,
        target_peer_id=payload.target.peer_id,
        route_binding_hash=route_hash,
    )


def _control_snapshot(route, payload, resolved) -> CloneTargetExecutionSnapshot:
    raw = f"{route.route_binding_hash}:target_control:{resolved.control_authorization.id}:1"
    return CloneTargetExecutionSnapshot(
        route_snapshot_id=route.id,
        execution_role="target_control",
        account_id=payload.target.control_account_id,
        authorization_id=payload.target.control_authorization_id,
        session_generation=resolved.control_authorization.slot_generation,
        execution_binding_hash=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
    )


__all__ = ["initialize_start_rows"]

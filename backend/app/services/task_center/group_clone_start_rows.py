from __future__ import annotations

import hashlib

from sqlalchemy import select

from app.models.group_clone import (
    CloneAccountSlot,
    CloneSourceStreamState,
    CloneTargetExecutionSnapshot,
    CloneTargetRouteSnapshot,
)
from app.models.telegram_updates import TelegramAuthorizationUpdateSubscription


def initialize_start_rows(session, task, *, payload, resolved, route_hash) -> None:
    stream = _ensure_stream(session, task, payload=payload, resolved=resolved)
    _prepare_stream(stream, resolved=resolved)
    subscription = _ensure_subscription(
        session, task, payload=payload, resolved=resolved,
    )
    _prepare_subscription(subscription, stream=stream, resolved=resolved)
    route = _ensure_route(session, task, payload=payload, route_hash=route_hash)
    session.flush()
    _ensure_control_snapshot(session, route, payload=payload, resolved=resolved)
    for authorization in resolved.sender_authorizations:
        _ensure_account_slot(session, task, authorization)


def _ensure_stream(session, task, *, payload, resolved):
    stream = session.scalar(select(CloneSourceStreamState).where(
        CloneSourceStreamState.task_id == task.id,
        CloneSourceStreamState.task_lifecycle_epoch == task.task_lifecycle_epoch,
    ).with_for_update())
    if stream is not None:
        return stream
    stream = CloneSourceStreamState(
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
    )
    session.add(stream)
    return stream


def _prepare_stream(stream, *, resolved) -> None:
    stream.authorization_update_state_id = resolved.listener_update_state.id
    stream.owner_id = None
    stream.lease_expires_at = None
    stream.state = "catching_up" if int(stream.start_pts or 0) > 0 else "initializing"
    if stream.state == "initializing":
        stream.last_consumed_ingress_order_no = resolved.listener_update_state.last_ingress_order_no
    stream.version = int(stream.version or 1) + 1


def _ensure_subscription(session, task, *, payload, resolved):
    subscription = session.scalar(select(TelegramAuthorizationUpdateSubscription).where(
        TelegramAuthorizationUpdateSubscription.task_id == task.id,
        TelegramAuthorizationUpdateSubscription.task_epoch == task.task_lifecycle_epoch,
    ).with_for_update())
    if subscription is not None:
        return subscription
    subscription = TelegramAuthorizationUpdateSubscription(
        authorization_update_state_id=resolved.listener_update_state.id,
        task_id=task.id,
        task_epoch=task.task_lifecycle_epoch,
        source_peer_type=payload.source.peer_type,
        source_peer_id=payload.source.peer_id,
        start_ingress_order=resolved.listener_update_state.last_ingress_order_no,
        state="initializing",
    )
    session.add(subscription)
    return subscription


def _prepare_subscription(subscription, *, stream, resolved) -> None:
    subscription.authorization_update_state_id = resolved.listener_update_state.id
    subscription.state = "active" if int(stream.start_pts or 0) > 0 else "initializing"
    if subscription.state == "initializing":
        subscription.start_ingress_order = resolved.listener_update_state.last_ingress_order_no
    subscription.version = int(subscription.version or 1) + 1


def _ensure_route(session, task, *, payload, route_hash):
    route = session.scalar(select(CloneTargetRouteSnapshot).where(
        CloneTargetRouteSnapshot.task_id == task.id,
        CloneTargetRouteSnapshot.epoch == task.task_lifecycle_epoch,
        CloneTargetRouteSnapshot.route_binding_version == 1,
    ).with_for_update())
    if route is not None:
        route.config_revision = task.config_revision
        return route
    route = _route_snapshot(task, payload, route_hash)
    session.add(route)
    return route


def _ensure_control_snapshot(session, route, *, payload, resolved) -> None:
    snapshot = session.scalar(select(CloneTargetExecutionSnapshot).where(
        CloneTargetExecutionSnapshot.route_snapshot_id == route.id,
        CloneTargetExecutionSnapshot.execution_binding_version == 1,
    ))
    if snapshot is None:
        session.add(_control_snapshot(route, payload, resolved))


def _ensure_account_slot(session, task, authorization) -> None:
    slot = session.scalar(select(CloneAccountSlot).where(
        CloneAccountSlot.task_id == task.id,
        CloneAccountSlot.account_id == authorization.account_id,
    ).with_for_update())
    if slot is None:
        session.add(CloneAccountSlot(
            task_id=task.id,
            account_id=authorization.account_id,
            authorization_id=authorization.id,
        ))
        return
    slot.authorization_id = authorization.id
    slot.state = "available"
    slot.projected_transport_blocked_until = None
    slot.owner_id = None
    slot.lease_expires_at = None
    slot.version = int(slot.version or 1) + 1


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

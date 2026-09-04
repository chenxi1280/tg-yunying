from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import (
    AccountStatus,
    Task,
    TelegramAuthorizationUpdateDelivery,
    TelegramAuthorizationUpdateState,
    TelegramAuthorizationUpdateSubscription,
    TgAccount,
    TgAccountAuthorization,
)

from .telegram_update_ingress import get_or_create_authorization_update_state


@dataclass(frozen=True)
class UpdateSubscriptionResult:
    ready: bool
    state: TelegramAuthorizationUpdateState | None = None
    subscription: TelegramAuthorizationUpdateSubscription | None = None
    route_changed: bool = False
    error: str = ""


def ensure_task_peer_update_subscription(
    session: Session,
    task: Task,
    *,
    listener_account_id: int,
    source_peer_id: str,
) -> UpdateSubscriptionResult:
    account, authorization = _listener_authorization(
        session,
        task,
        listener_account_id,
    )
    if account is None or authorization is None:
        return UpdateSubscriptionResult(False, error="listener_authorization_missing")
    state = get_or_create_authorization_update_state(
        session,
        task.tenant_id,
        account_id=account.id,
        authorization_id=authorization.id,
        session_generation=authorization.slot_generation,
    )
    _retire_old_subscriptions(session, task)
    subscription = _current_subscription(session, task)
    if subscription is None:
        subscription = _new_subscription(task, source_peer_id, state)
        session.add(subscription)
        route_changed = False
    else:
        route_changed = _rebind_subscription(
            session,
            task,
            subscription,
            source_peer_id,
            state,
        )
    session.flush()
    return UpdateSubscriptionResult(
        True,
        state=state,
        subscription=subscription,
        route_changed=route_changed,
    )


def _listener_authorization(session, task, account_id):
    account = session.get(TgAccount, account_id)
    if not _active_task_account(task, account):
        return None, None
    authorization = (
        session.get(TgAccountAuthorization, account.current_authorization_id)
        if account.current_authorization_id else None
    )
    if not _active_current_authorization(task, account, authorization):
        return account, None
    return account, authorization


def _active_task_account(task, account) -> bool:
    return bool(
        account
        and account.tenant_id == task.tenant_id
        and account.status == AccountStatus.ACTIVE.value
        and account.deleted_at is None
    )


def _active_current_authorization(task, account, authorization) -> bool:
    return bool(
        authorization
        and authorization.tenant_id == task.tenant_id
        and authorization.account_id == account.id
        and authorization.is_current
        and authorization.is_slot_current
        and authorization.status == "active"
        and authorization.session_ciphertext
    )


def _retire_old_subscriptions(session: Session, task: Task) -> None:
    session.execute(
        update(TelegramAuthorizationUpdateSubscription)
        .where(
            TelegramAuthorizationUpdateSubscription.task_id == task.id,
            TelegramAuthorizationUpdateSubscription.task_epoch
            != int(task.task_lifecycle_epoch or 1),
            TelegramAuthorizationUpdateSubscription.state.in_(("initializing", "active")),
        )
        .values(state="stopped")
    )


def _current_subscription(session, task):
    return session.scalar(select(TelegramAuthorizationUpdateSubscription).where(
        TelegramAuthorizationUpdateSubscription.task_id == task.id,
        TelegramAuthorizationUpdateSubscription.task_epoch
        == int(task.task_lifecycle_epoch or 1),
    ).with_for_update())


def _new_subscription(task, source_peer_id, state):
    return TelegramAuthorizationUpdateSubscription(
        authorization_update_state_id=state.id,
        task_id=task.id,
        task_epoch=int(task.task_lifecycle_epoch or 1),
        source_peer_type=_peer_type(source_peer_id),
        source_peer_id=str(source_peer_id),
        start_ingress_order=int(state.last_ingress_order_no or 0),
        state="active",
    )


def _rebind_subscription(session, task, subscription, source_peer_id, state) -> bool:
    peer_type = _peer_type(source_peer_id)
    peer_id = str(source_peer_id)
    route_changed = (
        subscription.authorization_update_state_id != state.id
        or subscription.source_peer_type != peer_type
        or subscription.source_peer_id != peer_id
    )
    if route_changed:
        _skip_pending_deliveries(session, task, subscription)
        subscription.authorization_update_state_id = state.id
        subscription.start_ingress_order = int(state.last_ingress_order_no or 0)
    changed = route_changed or subscription.state != "active"
    subscription.source_peer_type = peer_type
    subscription.source_peer_id = peer_id
    subscription.state = "active"
    if changed:
        subscription.version = int(subscription.version or 1) + 1
    return route_changed


def _skip_pending_deliveries(session, task, subscription) -> None:
    session.execute(
        update(TelegramAuthorizationUpdateDelivery)
        .where(
            TelegramAuthorizationUpdateDelivery.task_id == task.id,
            TelegramAuthorizationUpdateDelivery.subscription_id == subscription.id,
            TelegramAuthorizationUpdateDelivery.delivery_state == "pending",
        )
        .values(delivery_state="skipped")
    )


def _peer_type(peer_id: str) -> str:
    return "channel" if str(peer_id).startswith("-100") else "chat"


__all__ = [
    "UpdateSubscriptionResult",
    "ensure_task_peer_update_subscription",
]

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    GroupContextMessage,
    Task,
    TelegramAuthorizationUpdateDelivery,
    TelegramAuthorizationUpdateEvent,
    TelegramAuthorizationUpdateState,
    TelegramAuthorizationUpdateSubscription,
    TgGroup,
)
from app.services._common import _now
from app.timezone import as_beijing
from app.services.group_context_messages import try_insert_context_message
from app.services.group_listener_sender_identity import listener_ignored_sender

from .engagement_conversation import (
    apply_group_context_message_change,
    project_group_context_message,
)
from .engagement_unowned_activity import observe_managed_outbound
from .engagement_update_subscriptions import ensure_task_peer_update_subscription
from .negative_outcome_events import observe_human_negative_reply


UNIFIED_ENGAGEMENT_CONTRACT = "unified_engagement_v1"
CONSUMABLE_EVENT_TYPES = frozenset({"message_new"})
RECONCILE_EVENT_TYPES = frozenset({"message_delete"})


def ensure_group_ai_update_subscription(
    session: Session,
    task: Task,
    group: TgGroup,
    *,
    listener_account_id: int,
) -> bool:
    if not _uses_update_stream(task):
        return False
    result = ensure_task_peer_update_subscription(
        session,
        task,
        listener_account_id=listener_account_id,
        source_peer_id=str(group.tg_peer_id),
    )
    if not result.ready:
        _project_stream_state(task, "blocked", result.error)
        return False
    if result.route_changed:
        _mark_reconcile_required(task, group, "listener_route_changed")
    _project_stream_state(task, result.state.state, "")
    session.flush()
    return True


def consume_group_ai_update_deliveries(
    session: Session,
    task: Task,
    group: TgGroup,
    *,
    limit: int = 200,
) -> int:
    if not _uses_update_stream(task):
        return 0
    rows = session.execute(_delivery_statement(task, limit)).all()
    return sum(
        _consume_delivery(
            session, task=task, group=group, delivery=delivery,
            event=event, state=state,
        )
        for delivery, event, state in rows
    )


def _consume_delivery(
    session,
    *,
    task,
    group,
    delivery,
    event,
    state,
) -> int:
    payload = dict(delivery.normalized_payload or {})
    event_type = str(payload.get("event_type") or "")
    if event_type in RECONCILE_EVENT_TYPES:
        apply_group_context_message_change(
            session, task, group, payload=payload,
            deleted=event_type == "message_delete",
        )
        _mark_reconcile_required(task, group, event_type)
        delivery.delivery_state = "consumed"
        _project_delivery(task, event, clear_reconcile=False)
        return 0
    if event_type not in CONSUMABLE_EVENT_TYPES:
        delivery.delivery_state = "skipped"
        return 0
    if observe_managed_outbound(
        session, tenant_id=task.tenant_id,
        canonical_peer_id=str(group.tg_peer_id), payload=payload,
        action_class="authored_message", source_event_id=event.id,
    ):
        delivery.delivery_state = "skipped"
        _project_delivery(task, event)
        return 0
    message = _context_message(group, state, payload, event=event)
    if listener_ignored_sender(
        session, group, _sender_snapshot(message, payload),
    ):
        delivery.delivery_state = "skipped"
        return 0
    observe_human_negative_reply(session, task, peer_id=str(group.tg_peer_id), payload=payload)
    inserted = int(try_insert_context_message(session, message))
    if inserted:
        project_group_context_message(session, group, message)
    delivery.delivery_state = "consumed"
    _project_delivery(task, event)
    return inserted


def _uses_update_stream(task: Task) -> bool:
    return bool(
        task.type == "group_ai_chat"
        and task.status in {"pending", "running"}
        and (task.type_config or {}).get("engagement_contract_version")
        == UNIFIED_ENGAGEMENT_CONTRACT
    )


def _delivery_statement(task: Task, limit: int):
    return (
        select(
            TelegramAuthorizationUpdateDelivery,
            TelegramAuthorizationUpdateEvent,
            TelegramAuthorizationUpdateState,
        )
        .join(
            TelegramAuthorizationUpdateEvent,
            TelegramAuthorizationUpdateEvent.id
            == TelegramAuthorizationUpdateDelivery.update_event_id,
        )
        .join(
            TelegramAuthorizationUpdateSubscription,
            TelegramAuthorizationUpdateSubscription.id
            == TelegramAuthorizationUpdateDelivery.subscription_id,
        )
        .join(
            TelegramAuthorizationUpdateState,
            TelegramAuthorizationUpdateState.id
            == TelegramAuthorizationUpdateEvent.authorization_update_state_id,
        )
        .where(
            TelegramAuthorizationUpdateDelivery.task_id == task.id,
            TelegramAuthorizationUpdateDelivery.delivery_state == "pending",
            TelegramAuthorizationUpdateSubscription.task_epoch
            == int(task.task_lifecycle_epoch or 1),
            TelegramAuthorizationUpdateSubscription.state == "active",
        )
        .order_by(
            TelegramAuthorizationUpdateEvent.ingress_order_no,
            TelegramAuthorizationUpdateDelivery.normalized_item_index,
        )
        .limit(max(1, int(limit)))
        .with_for_update()
    )


def _context_message(group, state, payload, *, event) -> GroupContextMessage:
    return GroupContextMessage(
        tenant_id=group.tenant_id,
        group_id=group.id,
        listener_account_id=state.account_id,
        sender_peer_id=str(payload.get("sender_peer_id") or ""),
        sender_name=str(payload.get("sender_name") or "真人用户"),
        is_bot=bool(payload.get("sender_is_bot", False)),
        sender_role="bot" if payload.get("sender_is_bot") else "member",
        content=str(payload.get("content") or "")[:4000],
        message_type=str(payload.get("media_type") or "text"),
        remote_message_id=str(payload.get("source_message_id") or ""),
        sent_at=_sent_at(payload.get("sent_at"), event.observed_at),
    )


def _sender_snapshot(message: GroupContextMessage, payload: dict):
    return SimpleNamespace(
        remote_message_id=message.remote_message_id,
        sender_peer_id=message.sender_peer_id,
        sender_peer_type=str(payload.get("sender_peer_type") or "user"),
        sender_name=message.sender_name,
        sender_username="",
    )


def _sent_at(value, observed_at: datetime) -> datetime:
    parsed = datetime.fromisoformat(str(value)) if value else observed_at
    return as_beijing(parsed)


def _mark_reconcile_required(task: Task, group: TgGroup, event_type: str) -> None:
    from .listener_runtime import invalidate_listener_collect

    invalidate_listener_collect("group", group.id)
    task.stats = {
        **dict(task.stats or {}),
        "group_update_stream_reconcile_required": event_type,
    }


def _project_stream_state(task: Task, state: str, error: str) -> None:
    task.stats = {
        **dict(task.stats or {}),
        "group_update_stream_state": state,
        "group_update_stream_error": error,
    }


def _project_delivery(
    task: Task,
    event: TelegramAuthorizationUpdateEvent,
    *,
    clear_reconcile: bool = True,
) -> None:
    stats = dict(task.stats or {})
    stats["group_update_stream_last_ingress_order_no"] = event.ingress_order_no
    stats["group_update_stream_last_event_at"] = _now().isoformat()
    if clear_reconcile:
        stats.pop("group_update_stream_reconcile_required", None)
    task.stats = stats


__all__ = [
    "consume_group_ai_update_deliveries",
    "ensure_group_ai_update_subscription",
]

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.telegram_updates import (
    TelegramAuthorizationUpdateDelivery,
    TelegramAuthorizationUpdateEvent,
    TelegramAuthorizationUpdateState,
    TelegramAuthorizationUpdateSubscription,
    TelegramOutboundRandomIdMapping,
)
from app.services._common import _now

logger = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class NormalizedUpdateIngress:
    update_identity_key: str
    constructor_name: str
    pts_evidence: int | None
    pts_count_evidence: int | None
    routing_peer_type: str | None
    routing_peer_id: str | None
    normalized_items: tuple[dict[str, Any], ...]
    cursor_scope: str = "common_pts"


def compute_payload_fingerprint(payload: dict) -> str:
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def get_or_create_authorization_update_state(
    session: Session,
    tenant_id: int,
    *,
    account_id: int,
    authorization_id: int,
    session_generation: int = 1,
) -> TelegramAuthorizationUpdateState:
    """
    获取或创建账号授权维度的 Update 权威状态（单 generation 唯一）。
    """
    stmt = (
        select(TelegramAuthorizationUpdateState)
        .where(
            TelegramAuthorizationUpdateState.tenant_id == tenant_id,
            TelegramAuthorizationUpdateState.authorization_id == authorization_id,
            TelegramAuthorizationUpdateState.session_generation == session_generation,
        )
        .with_for_update()
    )
    state = session.execute(stmt).scalar_one_or_none()
    if not state:
        state = TelegramAuthorizationUpdateState(
            tenant_id=tenant_id,
            account_id=account_id,
            authorization_id=authorization_id,
            session_generation=session_generation,
            state="initializing",
            version=1,
        )
        session.add(state)
        session.flush()
    return state


def subscribe_task_to_updates(
    session: Session,
    state_id: str,
    task_id: str,
    *,
    task_epoch: int,
    source_peer_type: str,
    source_peer_id: str,
    start_ingress_order: int = 0,
) -> TelegramAuthorizationUpdateSubscription:
    """
    为任务创建或获取特定群的 Update 订阅。
    """
    stmt = (
        select(TelegramAuthorizationUpdateSubscription)
        .where(
            TelegramAuthorizationUpdateSubscription.task_id == task_id,
            TelegramAuthorizationUpdateSubscription.task_epoch == task_epoch,
        )
        .with_for_update()
    )
    sub = session.execute(stmt).scalar_one_or_none()
    if not sub:
        sub = TelegramAuthorizationUpdateSubscription(
            authorization_update_state_id=state_id,
            task_id=task_id,
            task_epoch=task_epoch,
            source_peer_type=source_peer_type,
            source_peer_id=source_peer_id,
            start_ingress_order=start_ingress_order,
            state="initializing",
            version=1,
        )
        session.add(sub)
        session.flush()
    return sub


def ingest_normalized_update(
    session: Session,
    state_id: str,
    ingress: NormalizedUpdateIngress,
    *,
    owner_id: str,
    owner_fencing_epoch: int,
) -> Tuple[TelegramAuthorizationUpdateEvent, List[TelegramAuthorizationUpdateDelivery]]:
    """原子持久化 UpdateEvent、匹配订阅 Deliveries 与共享游标。"""
    state_stmt = select(TelegramAuthorizationUpdateState).where(
        TelegramAuthorizationUpdateState.id == state_id
    ).with_for_update()
    state = session.execute(state_stmt).scalar_one()
    _validate_collector_owner(state, owner_id, owner_fencing_epoch)

    payload_summary = {
        "constructor": ingress.constructor_name,
        "pts": ingress.pts_evidence,
        "pts_count": ingress.pts_count_evidence,
        "peer_type": ingress.routing_peer_type,
        "peer_id": ingress.routing_peer_id,
        "items_count": len(ingress.normalized_items),
        "items": ingress.normalized_items,
    }
    payload_fingerprint = compute_payload_fingerprint(payload_summary)
    update_identity_hash = hashlib.sha256(
        f"{state_id}:{ingress.update_identity_key}".encode("utf-8")
    ).hexdigest()
    existing = session.scalar(select(TelegramAuthorizationUpdateEvent).where(
        TelegramAuthorizationUpdateEvent.authorization_update_state_id == state_id,
        TelegramAuthorizationUpdateEvent.update_identity_hash == update_identity_hash,
    ))
    if existing:
        if existing.payload_fingerprint != payload_fingerprint:
            raise ValueError("telegram_update_identity_payload_conflict")
        deliveries = list(session.scalars(select(TelegramAuthorizationUpdateDelivery).where(
            TelegramAuthorizationUpdateDelivery.update_event_id == existing.id,
        ).order_by(TelegramAuthorizationUpdateDelivery.normalized_item_index)))
        return existing, deliveries
    event = _create_update_event(
        session,
        state,
        ingress=ingress,
        update_identity_hash=update_identity_hash,
        payload_fingerprint=payload_fingerprint,
    )
    deliveries = _fanout_deliveries(session, state_id, ingress=ingress, event=event)
    _advance_shared_cursor(state, ingress, update_identity_hash)
    session.flush()
    return event, deliveries


def _validate_collector_owner(state, owner_id, owner_fencing_epoch) -> None:
    if state.state not in {"catching_up", "live"}:
        raise ValueError("telegram_update_collector_state_not_writable")
    if state.owner_id != owner_id or state.owner_fencing_epoch != owner_fencing_epoch:
        raise ValueError("telegram_update_collector_fenced")
    if state.lease_expires_at is None or state.lease_expires_at <= _now():
        raise ValueError("telegram_update_collector_lease_expired")


def _create_update_event(session, state, *, ingress, update_identity_hash, payload_fingerprint):
    state.last_ingress_order_no += 1
    event = TelegramAuthorizationUpdateEvent(
        authorization_update_state_id=state.id,
        ingress_order_no=state.last_ingress_order_no,
        update_identity_hash=update_identity_hash,
        constructor_name=ingress.constructor_name,
        pts_evidence=ingress.pts_evidence,
        pts_count_evidence=ingress.pts_count_evidence,
        routing_peer_type=ingress.routing_peer_type,
        routing_peer_id=ingress.routing_peer_id,
        payload_fingerprint=payload_fingerprint,
    )
    session.add(event)
    session.flush()
    return event


def _fanout_deliveries(session, state_id, *, ingress, event):
    sub_stmt = select(TelegramAuthorizationUpdateSubscription).where(
        TelegramAuthorizationUpdateSubscription.authorization_update_state_id == state_id,
        TelegramAuthorizationUpdateSubscription.source_peer_type == ingress.routing_peer_type,
        TelegramAuthorizationUpdateSubscription.source_peer_id == ingress.routing_peer_id,
        TelegramAuthorizationUpdateSubscription.state.in_(("initializing", "active")),
    )
    subscriptions = session.execute(sub_stmt).scalars().all()

    deliveries: List[TelegramAuthorizationUpdateDelivery] = []
    for sub in subscriptions:
        for idx, item in enumerate(ingress.normalized_items):
            item_fingerprint = compute_payload_fingerprint(item)
            delivery = TelegramAuthorizationUpdateDelivery(
                update_event_id=event.id,
                subscription_id=sub.id,
                task_id=sub.task_id,
                normalized_item_index=idx,
                normalized_payload=item,
                payload_fingerprint=item_fingerprint,
                delivery_state="pending",
            )
            session.add(delivery)
            deliveries.append(delivery)

    return deliveries


def _advance_shared_cursor(state, ingress, update_identity_hash) -> None:
    if ingress.cursor_scope == "event_only":
        state.last_update_identity_hash = update_identity_hash
        state.last_applied_at = _now()
        return
    if ingress.cursor_scope != "common_pts":
        raise ValueError("telegram_update_cursor_scope_invalid")
    pts = int(ingress.pts_evidence or 0)
    pts_count = int(ingress.pts_count_evidence or 0)
    if pts and pts_count and state.common_pts and pts - pts_count > state.common_pts:
        state.state = "gap"
    elif pts:
        state.common_pts = max(int(state.common_pts or 0), pts)
    state.last_update_identity_hash = update_identity_hash
    state.last_applied_at = _now()


def record_outbound_random_id_mapping(
    session: Session,
    state_id: str,
    *,
    random_id: int,
    target_peer_type: str,
    target_peer_id: str,
    remote_message_or_topic_id: str,
    action_id: Optional[str] = None,
    execution_attempt_id: Optional[str] = None,
    gateway_mutation_identity_id: Optional[str] = None,
    gateway_request_journal_id: Optional[str] = None,
    update_identity_hash: Optional[str] = None,
) -> TelegramOutboundRandomIdMapping:
    """记录 outbound random_id 到 remote_message_id 的权威映射。"""
    values = dict(
        state_id=state_id, random_id=random_id,
        target_peer_type=target_peer_type, target_peer_id=target_peer_id,
        remote_message_or_topic_id=remote_message_or_topic_id,
        action_id=action_id, execution_attempt_id=execution_attempt_id,
        gateway_mutation_identity_id=gateway_mutation_identity_id,
        gateway_request_journal_id=gateway_request_journal_id,
        update_identity_hash=update_identity_hash,
    )
    mapping = _find_outbound_mapping(session, state_id=state_id, random_id=random_id)
    if mapping is None:
        mapping = _new_outbound_mapping(**values)
        session.add(mapping)
        session.flush()
        return mapping
    _reconcile_outbound_mapping(mapping, values)
    return mapping


def _find_outbound_mapping(session, *, state_id, random_id):
    return session.scalar(select(TelegramOutboundRandomIdMapping).where(
        TelegramOutboundRandomIdMapping.authorization_update_state_id == state_id,
        TelegramOutboundRandomIdMapping.random_id == random_id,
    ).with_for_update())


def _reconcile_outbound_mapping(mapping, values) -> None:
    _validate_outbound_mapping(
        mapping,
        target_peer_type=values["target_peer_type"],
        target_peer_id=values["target_peer_id"],
        remote_message_or_topic_id=values["remote_message_or_topic_id"],
        gateway_mutation_identity_id=values["gateway_mutation_identity_id"],
    )
    _enrich_outbound_mapping(
        mapping,
        action_id=values["action_id"],
        execution_attempt_id=values["execution_attempt_id"],
        gateway_mutation_identity_id=values["gateway_mutation_identity_id"],
        gateway_request_journal_id=values["gateway_request_journal_id"],
        update_identity_hash=values["update_identity_hash"],
    )


def _new_outbound_mapping(**values) -> TelegramOutboundRandomIdMapping:
    return TelegramOutboundRandomIdMapping(
        authorization_update_state_id=values["state_id"],
        gateway_mutation_identity_id=values["gateway_mutation_identity_id"],
        random_id=values["random_id"],
        gateway_request_journal_id=values["gateway_request_journal_id"],
        action_id=values["action_id"],
        execution_attempt_id=values["execution_attempt_id"],
        target_peer_type=values["target_peer_type"],
        target_peer_id=values["target_peer_id"],
        remote_message_or_topic_id=str(values["remote_message_or_topic_id"]),
        update_identity_hash=values["update_identity_hash"],
    )


def _validate_outbound_mapping(
    mapping,
    *,
    target_peer_type,
    target_peer_id,
    remote_message_or_topic_id,
    gateway_mutation_identity_id,
) -> None:
    expected = (
        mapping.target_peer_type,
        mapping.target_peer_id,
        mapping.remote_message_or_topic_id,
    )
    observed = (
        target_peer_type,
        target_peer_id,
        str(remote_message_or_topic_id),
    )
    if expected != observed:
        raise ValueError("telegram_outbound_random_id_mapping_conflict")
    if (
        gateway_mutation_identity_id
        and mapping.gateway_mutation_identity_id
        and mapping.gateway_mutation_identity_id != gateway_mutation_identity_id
    ):
        raise ValueError("telegram_outbound_random_id_identity_conflict")


def _enrich_outbound_mapping(mapping, **values) -> None:
    for key, value in values.items():
        if not value:
            continue
        existing = getattr(mapping, key)
        if existing and existing != value:
            raise ValueError(f"telegram_outbound_random_id_{key}_conflict")
        if not existing:
            setattr(mapping, key, value)

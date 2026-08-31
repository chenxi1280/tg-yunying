from __future__ import annotations

import hashlib
import json
import random
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Action, Task, TgAccountAuthorization
from app.models.fulfillment_v2 import FulfillmentObligationProjection
from app.models.group_clone import (
    CloneDeliveryObligation,
    CloneMessagePart,
    CloneSenderBindingHistory,
    CloneSourceEvent,
    CloneSourceStreamState,
    CloneTargetExecutionSnapshot,
    CloneTargetRouteSnapshot,
    CloneTopicMap,
    TelegramGatewayMutationIdentity,
)
from app.schemas.task_center import GroupCloneConfig

from .group_clone_binding import CloneSenderBindingManager
from .group_clone_content import (
    CloneContentReviewRequired,
    clone_content_entities,
    sanitize_clone_content,
)
from .group_clone_media_materializer import (
    admit_media_events,
    allocate_send_identity,
    materialize_media_items,
)
from .payloads import GroupCloneSendPayload

GROUP_CLONE_CONTRACT = "v2_group_clone"


def materialize_ready_clone_events(session: Session, task: Task) -> int:
    if not _runtime_ready(session, task):
        return 0
    route = _current_route(session, task)
    if route is None:
        raise RuntimeError("group_clone_route_snapshot_missing")
    created = 0
    while True:
        waiting = _next_waiting_obligation(session, task)
        if waiting is not None:
            event = session.get(CloneSourceEvent, waiting.source_event_id)
            if event is None:
                raise RuntimeError("group_clone_waiting_source_event_missing")
            config = _event_config(task, event)
            created += int(_materialize_event(session, task, config=config, route=route, event=event, obligation=waiting))
            if waiting.state not in {"action_bound", "filtered", "cancelled", "superseded"}:
                return created
            continue
        event = _next_event(session, task)
        if event is None:
            return created
        config = _event_config(task, event)
        obligation = _new_obligation(session, task, config=config, route=route, event=event)
        created += int(_materialize_event(session, task, config=config, route=route, event=event, obligation=obligation))
        if obligation.state not in {"action_bound", "filtered", "cancelled", "superseded"}:
            return created


def _next_waiting_obligation(session, task):
    retryable = (
        "observed", "waiting_binding", "waiting_album",
        "waiting_dependency", "waiting_transport",
    )
    return session.scalar(select(CloneDeliveryObligation).where(
        CloneDeliveryObligation.task_id == task.id,
        CloneDeliveryObligation.epoch == task.task_lifecycle_epoch,
        CloneDeliveryObligation.state.in_(retryable),
    ).order_by(CloneDeliveryObligation.sequencer_id).with_for_update())


def _runtime_ready(session: Session, task: Task) -> bool:
    if task.status != "running" or (task.stats or {}).get("clone_start_state") != "running":
        return False
    stream = session.scalar(select(CloneSourceStreamState).where(
        CloneSourceStreamState.task_id == task.id,
        CloneSourceStreamState.task_lifecycle_epoch == task.task_lifecycle_epoch,
    ))
    return bool(stream and stream.state == "live")


def _current_route(session: Session, task: Task):
    return session.scalar(
        select(CloneTargetRouteSnapshot)
        .where(
            CloneTargetRouteSnapshot.task_id == task.id,
            CloneTargetRouteSnapshot.epoch == task.task_lifecycle_epoch,
        )
        .order_by(CloneTargetRouteSnapshot.route_binding_version.desc())
        .limit(1)
    )


def _next_event(session: Session, task: Task):
    last_order = session.scalar(select(func.max(CloneDeliveryObligation.stream_order_no)).where(
        CloneDeliveryObligation.task_id == task.id,
        CloneDeliveryObligation.epoch == task.task_lifecycle_epoch,
    )) or 0
    return session.scalar(select(CloneSourceEvent).where(
        CloneSourceEvent.task_id == task.id,
        CloneSourceEvent.task_lifecycle_epoch == task.task_lifecycle_epoch,
        CloneSourceEvent.stream_order_no == last_order + 1,
    ).with_for_update())


def _new_obligation(session, task, *, config, route, event):
    kind = "send" if event.event_type == "message_new" else event.event_type.removeprefix("message_")
    obligation = CloneDeliveryObligation(
        tenant_id=task.tenant_id,
        task_id=task.id,
        epoch=task.task_lifecycle_epoch,
        source_event_id=event.id,
        source_message_revision=event.message_revision,
        obligation_kind=kind,
        stream_order_no=event.stream_order_no,
        sequencer_id=event.stream_order_no,
        route_binding_snapshot_id=route.id,
        config_revision=event.config_revision,
        sanitization_revision=event.sanitization_revision,
        planned_at=_planned_at(session, task, config=config, stream_order_no=event.stream_order_no),
        unknown_deadline_at=datetime.now(timezone.utc) + timedelta(seconds=config.lifecycle.unknown_deadline_seconds),
        state="observed",
    )
    session.add(obligation)
    session.flush()
    return obligation


def _materialize_event(session, task, *, config, route, event, obligation):
    if event.event_type != "message_new":
        from .group_clone_lifecycle_materializer import materialize_lifecycle_event

        result = materialize_lifecycle_event(
            session, task, config=config, route=route,
            event=event, obligation=obligation,
        )
        if result != "resend":
            return result
    return _materialize_new_event(
        session, task, config=config, route=route, event=event, obligation=obligation,
    )


def _materialize_new_event(session, task, *, config, route, event, obligation):
    media_events = _new_content_events(
        session, task, config=config, event=event, obligation=obligation,
    )
    if media_events is None or not media_events:
        return False
    topic_target = _topic_target(session, task, event)
    if event.source_top_message_id and not topic_target:
        from .group_clone_lifecycle_materializer import materialize_topic_bootstrap

        return materialize_topic_bootstrap(
            session, task, route=route, event=event, obligation=obligation,
        )
    reply_target = _reply_target(session, task, event)
    reply_fallback = _resolve_reply_fallback(
        session, task, config=config, event=event,
        obligation=obligation, reply_target=reply_target,
    )
    if reply_fallback is None:
        return False
    quote_prefix, fallback_parent_sender = reply_fallback
    sanitized = _new_event_content(
        session, task, config=config, event=event,
        media_events=media_events, obligation=obligation,
    )
    if sanitized is None:
        return False
    sanitized = f"{quote_prefix}{sanitized}" if quote_prefix else sanitized
    parent_sender_id = _reply_sender_id(session, task, event) or fallback_parent_sender
    binding, reason = CloneSenderBindingManager.get_or_assign_sender_binding(
        session, task,
        source_sender_peer_type=event.sender_peer_type,
        source_sender_peer_id=event.sender_peer_id,
        source_sender_name=event.sender_name,
        reply_to_sender_peer_id=parent_sender_id,
    )
    if binding is None:
        obligation.state = "waiting_binding"
        obligation.error_code = reason
        return False
    execution = _execution_snapshot(session, route, binding)
    obligation.binding_history_id = binding.id
    obligation.execution_target_binding_snapshot_id = execution.id
    return _bind_send_action(
        session, task, route=route, execution=execution, event=event,
        obligation=obligation, content=sanitized, reply_target=reply_target,
        target_top_message_id=topic_target or route.target_top_msg_id,
        media_events=media_events if event.media_type != "text" else [],
        config=config,
    )


def _new_event_content(session, task, *, config, event, media_events, obligation):
    try:
        sanitized = sanitize_clone_content(
            session, task, config=config, event=event,
        )
    except CloneContentReviewRequired as exc:
        _block(obligation, str(exc))
        return None
    if sanitized is not None:
        return sanitized
    if any(item.media_type != "text" for item in media_events):
        return ""
    obligation.state = "filtered"
    obligation.resolved_at = datetime.now(timezone.utc)
    return None


def _new_content_events(session, task, *, config, event, obligation):
    if event.protected_content:
        _block(obligation, "protected_content")
        return None
    if (
        event.sender_peer_type == "channel"
        and event.sender_peer_id == event.source_peer_id
        and event.sender_name
    ):
        _block(obligation, "anonymous_sender_identity_unproven")
        return None
    if event.grouped_id:
        from .group_clone_album import prepare_album_events

        events = prepare_album_events(
            session,
            task,
            event=event,
            obligation=obligation,
            incomplete_policy=config.content.incomplete_album_policy,
        )
        return admit_media_events(
            events, obligation, policy=config.content.unsupported_media_policy,
        )
    if not event.sender_peer_type or not event.sender_peer_id:
        _block(obligation, "source_sender_identity_unproven")
        return None
    return admit_media_events(
        [event], obligation, policy=config.content.unsupported_media_policy,
    )


def _resolve_reply_fallback(
    session, task, *, config, event, obligation, reply_target,
):
    if not event.reply_to_message_id or reply_target is not None:
        return "", None
    from .group_clone_reply import resolve_orphan_reply

    resolution = resolve_orphan_reply(
        session, task, config=config, event=event,
        sanitize=lambda parent: sanitize_clone_content(
            session, task, config=config, event=parent,
        ),
    )
    if not resolution.terminal_state:
        obligation.degradation_reason = resolution.error_code or None
        return resolution.quote_prefix, resolution.parent_sender_peer_id
    obligation.state = resolution.terminal_state
    obligation.error_code = resolution.error_code
    if resolution.terminal_state == "filtered":
        obligation.resolved_at = datetime.now(timezone.utc)
    return None


def _execution_snapshot(session, route, binding):
    existing = session.scalar(select(CloneTargetExecutionSnapshot).where(
        CloneTargetExecutionSnapshot.route_snapshot_id == route.id,
        CloneTargetExecutionSnapshot.execution_role == "sender",
        CloneTargetExecutionSnapshot.sender_binding_history_id == binding.id,
    ))
    if existing:
        return existing
    authorization = session.scalar(select(TgAccountAuthorization).where(
        TgAccountAuthorization.account_id == binding.assigned_account_id,
        TgAccountAuthorization.is_current.is_(True),
        TgAccountAuthorization.status == "active",
    ))
    if authorization is None:
        raise RuntimeError("group_clone_sender_authorization_missing")
    version = session.scalar(select(func.max(CloneTargetExecutionSnapshot.execution_binding_version)).where(
        CloneTargetExecutionSnapshot.route_snapshot_id == route.id,
    )) or 0
    digest = _hash({"route": route.route_binding_hash, "binding": binding.id, "authorization": authorization.id})
    snapshot = CloneTargetExecutionSnapshot(
        route_snapshot_id=route.id,
        execution_binding_version=version + 1,
        execution_role="sender",
        account_id=binding.assigned_account_id,
        authorization_id=authorization.id,
        session_generation=authorization.slot_generation,
        sender_binding_history_id=binding.id,
        sender_binding_version=binding.binding_version,
        execution_binding_hash=digest,
    )
    session.add(snapshot)
    session.flush()
    return snapshot


def _bind_send_action(
    session, task, *, route, execution, event, obligation, content,
    reply_target, target_top_message_id, media_events, config,
):
    try:
        media_items = materialize_media_items(
            session, task, route=route, execution=execution,
            obligation=obligation, events=media_events, primary_content=content,
        )
    except CloneContentReviewRequired as exc:
        return _block(obligation, str(exc))
    identity = _primary_identity(
        session, task, route=route, execution=execution, event=event,
        obligation=obligation, content=content, media_items=media_items,
    )
    payload = _send_payload(
        session, config=config, route=route, execution=execution,
        event=event, obligation=obligation, identity=identity,
        content=content, reply_target=reply_target,
        target_top_message_id=target_top_message_id, media_items=media_items,
    )
    action = Action(
        tenant_id=task.tenant_id,
        task_id=task.id,
        task_type=task.type,
        action_type="group_clone_send",
        status="pending",
        account_id=execution.account_id,
        payload=payload.model_dump(mode="json"),
        scheduled_at=obligation.planned_at,
        action_dedupe_key=f"group_clone:{obligation.id}:{obligation.materialization_version}",
        obligation_type="group_clone_delivery",
        obligation_id=obligation.id,
        task_lifecycle_epoch=task.task_lifecycle_epoch,
        unknown_deadline_at=obligation.unknown_deadline_at,
    )
    session.add(action)
    session.flush()
    obligation.state = "action_bound"
    _mark_album_action_bound(session, obligation)
    _bind_projection(session, task, obligation=obligation, action=action)
    return True


def _send_payload(
    session, *, config, route, execution, event, obligation, identity,
    content, reply_target, target_top_message_id, media_items,
):
    source = session.get(TgAccountAuthorization, config.source.authorization_id)
    if media_items and source is None:
        raise RuntimeError("group_clone_media_source_authorization_missing")
    return GroupCloneSendPayload(
        obligation_id=obligation.id, gateway_mutation_identity_id=identity.id,
        route_snapshot_id=route.id, execution_snapshot_id=execution.id,
        mutation_kind=identity.mutation_kind, target_peer_type=route.target_peer_type,
        target_peer_id=route.target_peer_id, content=content,
        entities=clone_content_entities(event, content),
        random_id=identity.random_id, stream_order_no=event.stream_order_no,
        source_message_id=event.source_message_id, reply_to_message_id=reply_target,
        target_top_message_id=target_top_message_id,
        source_peer_id=route.source_peer_id if media_items else "",
        source_authorization_id=source.id if source and media_items else None,
        source_session_generation=source.slot_generation if source and media_items else None,
        media_items=media_items,
    )


def _primary_identity(
    session, task, *, route, execution, event, obligation, content, media_items,
):
    if media_items:
        identity = session.get(
            TelegramGatewayMutationIdentity,
            media_items[0].gateway_mutation_identity_id,
        )
        if identity is None:
            raise RuntimeError("group_clone_media_identity_missing")
        return identity
    return allocate_send_identity(
        session, task, route=route, execution=execution, event=event,
        obligation=obligation, content=content,
    )


def _mark_album_action_bound(session, obligation) -> None:
    if not obligation.album_manifest_id:
        return
    from app.models.group_clone import CloneAlbumManifest

    manifest = session.get(CloneAlbumManifest, obligation.album_manifest_id)
    if manifest is not None:
        manifest.state = "action_bound"
        manifest.version += 1


def _bind_projection(session, task, *, obligation, action):
    row = session.scalar(select(FulfillmentObligationProjection).where(
        FulfillmentObligationProjection.obligation_type == "group_clone_delivery",
        FulfillmentObligationProjection.obligation_id == obligation.id,
    ))
    if row:
        row.active_action_id = action.id
        row.state = "open"
        row.version += 1
        return
    session.add(FulfillmentObligationProjection(
        tenant_id=task.tenant_id,
        task_id=task.id,
        task_lifecycle_epoch=task.task_lifecycle_epoch,
        obligation_type="group_clone_delivery",
        obligation_id=obligation.id,
        work_lane="interaction",
        active_action_id=action.id,
    ))


def _planned_at(session, task, *, config, stream_order_no):
    previous = session.scalar(select(func.max(CloneDeliveryObligation.planned_at)).where(
        CloneDeliveryObligation.task_id == task.id,
        CloneDeliveryObligation.epoch == task.task_lifecycle_epoch,
        CloneDeliveryObligation.stream_order_no < stream_order_no,
    ))
    now_value = datetime.now(timezone.utc)
    if previous and previous.tzinfo is None:
        previous = previous.replace(tzinfo=timezone.utc)
    base = max(now_value, previous) if previous else now_value
    delay = random.uniform(config.pacing.min_delay_ms, config.pacing.max_delay_ms) / 1000
    return base + timedelta(seconds=delay)


def _event_config(task, event) -> GroupCloneConfig:
    snapshot = dict(event.config_snapshot or {})
    if not snapshot:
        raise RuntimeError("group_clone_event_config_snapshot_missing")
    return GroupCloneConfig.model_validate(snapshot)


def _reply_target(session, task, event):
    if not event.reply_to_message_id:
        return None
    return session.scalar(select(CloneMessagePart.target_message_id).where(
        CloneMessagePart.task_id == task.id,
        CloneMessagePart.epoch == task.task_lifecycle_epoch,
        CloneMessagePart.source_message_id == event.reply_to_message_id,
    ).limit(1))


def _reply_sender_id(session, task, event):
    if not event.reply_to_message_id:
        return None
    return session.scalar(select(CloneSourceEvent.sender_peer_id).where(
        CloneSourceEvent.task_id == task.id,
        CloneSourceEvent.task_lifecycle_epoch == task.task_lifecycle_epoch,
        CloneSourceEvent.source_message_id == event.reply_to_message_id,
    ).order_by(CloneSourceEvent.message_revision.desc()).limit(1))


def _topic_target(session, task, event):
    if not event.source_top_message_id:
        return None
    return session.scalar(select(CloneTopicMap.target_top_message_id).where(
        CloneTopicMap.task_id == task.id,
        CloneTopicMap.epoch == task.task_lifecycle_epoch,
        CloneTopicMap.source_peer_type == event.source_peer_type,
        CloneTopicMap.source_peer_id == event.source_peer_id,
        CloneTopicMap.source_top_message_id == event.source_top_message_id,
        CloneTopicMap.state == "ready",
    ))


def _block(obligation, code):
    obligation.state = "waiting_manual_review"
    obligation.error_code = code
    return False


def _hash(value) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


__all__ = ["materialize_ready_clone_events"]

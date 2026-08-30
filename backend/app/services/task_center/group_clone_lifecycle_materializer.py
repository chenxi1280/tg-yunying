from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models import Action, TgAccountAuthorization
from app.models.fulfillment_v2 import FulfillmentObligationProjection
from app.models.group_clone import (
    CloneDeliveryObligation,
    CloneMessagePart,
    CloneSourceEvent,
    CloneSourceStreamState,
    CloneTargetExecutionSnapshot,
    CloneTopicMap,
    TelegramGatewayMutationIdentity,
)
from app.services._common import gateway
from app.services.developer_apps import credentials_for_authorization

from .group_clone_identity import derive_deterministic_random_id
from .payloads import GroupCloneMutationPayload

GROUP_CLONE_CONTRACT = "v2_group_clone"


def materialize_lifecycle_event(session, task, *, route, event, obligation):
    if event.event_type == "message_delete" and _deleted_topic_map(session, task, event):
        return _materialize_topic(
            session, task, route=route, event=event, obligation=obligation,
        )
    if event.event_type == "message_edit":
        return _materialize_edit(session, task, route=route, event=event, obligation=obligation)
    if event.event_type == "message_delete":
        return _materialize_delete(session, task, route=route, event=event, obligation=obligation)
    if event.event_type == "message_pin":
        return _materialize_pin(session, task, route=route, event=event, obligation=obligation)
    if event.event_type in {"topic_create", "topic_edit", "topic_delete"}:
        return _materialize_topic(session, task, route=route, event=event, obligation=obligation)
    obligation.state = "waiting_manual_review"
    obligation.error_code = "lifecycle_event_unsupported"
    return False


def materialize_topic_bootstrap(session, task, *, route, event, obligation):
    source_top = int(event.source_top_message_id or 0)
    if source_top <= 0:
        raise RuntimeError("group_clone_topic_source_top_missing")
    topic = _topic_map(session, task, event=event, source_top=source_top)
    obligation.topic_map_id = topic.id
    if topic.state == "ready" and topic.target_top_message_id:
        return False
    if topic.state == "creating":
        return _wait(obligation, "topic_bootstrap_in_progress")
    source_topic = _fresh_source_topic(session, task, event, source_top=source_top)
    if source_topic is None:
        obligation.state = "waiting_manual_review"
        obligation.error_code = "topic_lazy_fetch_unproven"
        topic.state = "blocked"
        return False
    topic.topic_title_fingerprint = _hash(source_topic["title"])
    topic.topic_icon_fingerprint = _hash({
        "icon_color": source_topic.get("icon_color"),
        "icon_emoji_id": source_topic.get("icon_emoji_id"),
    })
    topic.state = "creating"
    return _bind_control_mutation(
        session, task, route=route, event=event, obligation=obligation,
        mutation_kind="createForumTopic", target_ids=[],
        content=source_topic["title"], random_required=True,
        resume_obligation_after_success=True,
        topic_options=source_topic,
    )


def _materialize_edit(session, task, *, route, event, obligation):
    original = _original_obligation(session, task, event.source_message_id)
    if original is None:
        return _wait(obligation, "edit_original_obligation_missing")
    action = _active_action(session, original)
    if action is not None and action.status == "pending":
        action.status = "cancelled"
        action.result = {"reason": "superseded_by_source_edit_before_send"}
        original.state = "superseded"
        original.resolved_at = datetime.now(timezone.utc)
        _close_original_projection(session, original)
        return "resend"
    part = _message_part(session, task, event.source_message_id)
    if part is None:
        return _wait_for_original(obligation, original, "edit_target_mapping_required")
    execution = _sender_execution(session, route.id, part)
    return _bind_mutation(
        session, task, route=route, execution=execution, event=event,
        obligation=obligation, mutation_kind="editMessage",
        target_ids=[part.target_message_id], content=event.content,
        entities=event.entities,
    )


def _materialize_delete(session, task, *, route, event, obligation):
    original = _original_obligation(session, task, event.source_message_id)
    action = _active_action(session, original) if original else None
    if action is not None and action.status == "pending":
        action.status = "cancelled"
        action.result = {"reason": "cancelled_by_source_delete_before_send"}
        original.state = "cancelled"
        original.resolved_at = datetime.now(timezone.utc)
        _close_original_projection(session, original)
        obligation.state = "cancelled"
        obligation.resolved_at = datetime.now(timezone.utc)
        return False
    part = _message_part(session, task, event.source_message_id)
    if part is None:
        return _wait_for_original(obligation, original, "delete_target_mapping_required")
    return _bind_control_mutation(
        session, task, route=route, event=event, obligation=obligation,
        mutation_kind="deleteMessages", target_ids=[part.target_message_id],
    )


def _materialize_pin(session, task, *, route, event, obligation):
    part = _message_part(session, task, event.source_message_id)
    if part is None:
        return _wait(obligation, "pin_target_mapping_required")
    pinned = bool((event.poll_snapshot or {}).get("pinned", True))
    return _bind_control_mutation(
        session, task, route=route, event=event, obligation=obligation,
        mutation_kind="pinMessage" if pinned else "unpinMessage",
        target_ids=[part.target_message_id],
    )


def _materialize_topic(session, task, *, route, event, obligation):
    source_top = int(event.source_top_message_id or event.source_message_id)
    topic = _topic_map(session, task, event=event, source_top=source_top)
    obligation.topic_map_id = topic.id
    if event.event_type == "topic_create":
        topic.state = "creating"
        return _bind_control_mutation(
            session, task, route=route, event=event, obligation=obligation,
            mutation_kind="createForumTopic", target_ids=[],
            content=_topic_title(event), random_required=True,
        )
    if topic.state != "ready" or not topic.target_top_message_id:
        return _wait(obligation, "topic_target_mapping_required")
    mutation = "editForumTopic" if event.event_type == "topic_edit" else "deleteForumTopic"
    return _bind_control_mutation(
        session, task, route=route, event=event, obligation=obligation,
        mutation_kind=mutation, target_ids=[topic.target_top_message_id],
        content=_topic_title(event, default="") if mutation == "editForumTopic" else "",
        topic_closed=(event.poll_snapshot or {}).get("closed") if mutation == "editForumTopic" else None,
        topic_options=event.poll_snapshot if mutation == "editForumTopic" else None,
    )


def _bind_control_mutation(
    session, task, *, route, event, obligation, mutation_kind,
    target_ids, content="", random_required=False,
    resume_obligation_after_success=False,
    topic_closed=None, topic_options=None,
):
    execution = session.scalar(select(CloneTargetExecutionSnapshot).where(
        CloneTargetExecutionSnapshot.route_snapshot_id == route.id,
        CloneTargetExecutionSnapshot.execution_role == "target_control",
    ))
    if execution is None:
        raise RuntimeError("group_clone_control_execution_snapshot_missing")
    return _bind_mutation(
        session, task, route=route, execution=execution, event=event,
        obligation=obligation, mutation_kind=mutation_kind,
        target_ids=target_ids, content=content, entities=[],
        random_required=random_required,
        resume_obligation_after_success=resume_obligation_after_success,
        topic_closed=topic_closed,
        topic_options=topic_options,
    )


def _bind_mutation(
    session, task, *, route, execution, event, obligation, mutation_kind,
    target_ids, content, entities, random_required=False,
    resume_obligation_after_success=False,
    topic_closed=None, topic_options=None,
):
    topic_options = dict(topic_options or {})
    identity = _allocate_identity(
        session, task, route=route, execution=execution, obligation=obligation,
        mutation_kind=mutation_kind, request={
            "target_ids": target_ids, "content": content, "entities": entities,
            "topic_closed": topic_closed,
            "topic_options": topic_options,
        }, random_required=random_required,
    )
    payload = GroupCloneMutationPayload(
        obligation_id=obligation.id,
        gateway_mutation_identity_id=identity.id,
        route_snapshot_id=route.id,
        execution_snapshot_id=execution.id,
        mutation_kind=mutation_kind,
        execution_role=execution.execution_role,
        target_peer_type=route.target_peer_type,
        target_peer_id=route.target_peer_id,
        source_message_id=int(event.source_top_message_id or event.source_message_id),
        target_message_ids=target_ids,
        content=content,
        entities=entities,
        random_id=identity.random_id,
        target_top_message_id=route.target_top_msg_id,
        resume_obligation_after_success=resume_obligation_after_success,
        topic_closed=topic_closed,
        topic_hidden=topic_options.get("hidden"),
        topic_icon_color=topic_options.get("icon_color"),
        topic_icon_emoji_id=int(topic_options["icon_emoji_id"]) if topic_options.get("icon_emoji_id") else None,
    )
    action = _new_mutation_action(
        task, execution, obligation=obligation, payload=payload,
    )
    session.add(action)
    session.flush()
    obligation.execution_target_binding_snapshot_id = execution.id
    obligation.state = "action_bound"
    _bind_projection(session, task, obligation=obligation, action=action)
    return True


def _deleted_topic_map(session, task, event):
    return session.scalar(select(CloneTopicMap).where(
        CloneTopicMap.task_id == task.id,
        CloneTopicMap.epoch == task.task_lifecycle_epoch,
        CloneTopicMap.source_top_message_id == event.source_message_id,
        CloneTopicMap.state.in_(("ready", "creating", "unknown")),
    ))


def _new_mutation_action(task, execution, *, obligation, payload):
    return Action(
        tenant_id=task.tenant_id,
        task_id=task.id,
        task_type=task.type,
        action_type="group_clone_mutation",
        status="pending",
        account_id=execution.account_id,
        payload=payload.model_dump(mode="json"),
        scheduled_at=obligation.planned_at,
        action_dedupe_key=(
            f"group_clone:{obligation.id}:{payload.mutation_kind}:"
            f"{obligation.materialization_version}"
        ),
        obligation_type="group_clone_delivery",
        obligation_id=obligation.id,
        task_lifecycle_epoch=task.task_lifecycle_epoch,
        unknown_deadline_at=obligation.unknown_deadline_at,
    )


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


def _allocate_identity(
    session, task, *, route, execution, obligation, mutation_kind,
    request, random_required,
):
    existing = session.scalar(select(TelegramGatewayMutationIdentity).where(
        TelegramGatewayMutationIdentity.obligation_id == obligation.id,
        TelegramGatewayMutationIdentity.mutation_kind == mutation_kind,
    ))
    fingerprint = _hash(request)
    if existing:
        if existing.request_fingerprint != fingerprint:
            raise RuntimeError("group_clone_mutation_identity_fingerprint_conflict")
        return existing
    authorization = session.get(TgAccountAuthorization, execution.authorization_id)
    account_peer = str(authorization.telegram_user_id_digest or "") if authorization else ""
    if not account_peer:
        raise RuntimeError("canonical_telegram_account_peer_id_unproven")
    return _insert_identity(
        session, task, route=route, execution=execution, obligation=obligation,
        mutation_kind=mutation_kind, fingerprint=fingerprint,
        account_peer=account_peer, random_required=random_required,
    )


def _insert_identity(
    session, task, *, route, execution, obligation, mutation_kind,
    fingerprint, account_peer, random_required,
):
    nonce = 0
    while True:
        random_id = _random_id(
            task=task, obligation=obligation, mutation_kind=mutation_kind, nonce=nonce,
        ) if random_required else None
        identity = TelegramGatewayMutationIdentity(
            tenant_id=task.tenant_id,
            task_id=task.id,
            epoch=task.task_lifecycle_epoch,
            obligation_id=obligation.id,
            mutation_kind=mutation_kind,
            execution_role=execution.execution_role,
            account_id=execution.account_id,
            telegram_account_peer_id=account_peer,
            authorization_id=execution.authorization_id,
            session_generation=execution.session_generation,
            target_peer_type=route.target_peer_type,
            target_peer_id=route.target_peer_id,
            random_id=random_id,
            collision_nonce=nonce,
            request_fingerprint=fingerprint,
        )
        try:
            with session.begin_nested():
                session.add(identity)
                session.flush()
            return identity
        except IntegrityError:
            nonce += 1


def _random_id(*, task, obligation, mutation_kind, nonce):
    return derive_deterministic_random_id(
        GROUP_CLONE_CONTRACT,
        task.tenant_id,
        task_id=task.id,
        epoch=task.task_lifecycle_epoch,
        obligation_id=obligation.id,
        mutation_kind=mutation_kind,
        part_index=0,
        collision_nonce=nonce,
    )


def _original_obligation(session, task, source_message_id):
    return session.scalar(
        select(CloneDeliveryObligation)
        .join(CloneSourceEvent, CloneSourceEvent.id == CloneDeliveryObligation.source_event_id)
        .where(
            CloneDeliveryObligation.task_id == task.id,
            CloneDeliveryObligation.epoch == task.task_lifecycle_epoch,
            CloneSourceEvent.source_message_id == source_message_id,
            CloneSourceEvent.event_type == "message_new",
        )
        .order_by(CloneDeliveryObligation.materialization_version.desc())
        .limit(1)
    )


def _active_action(session, obligation):
    if obligation is None:
        return None
    return session.scalar(select(Action).where(
        Action.obligation_id == obligation.id,
    ).order_by(Action.created_at.desc()).limit(1))


def _close_original_projection(session, obligation):
    row = session.scalar(select(FulfillmentObligationProjection).where(
        FulfillmentObligationProjection.obligation_type == "group_clone_delivery",
        FulfillmentObligationProjection.obligation_id == obligation.id,
    ))
    if row:
        row.state = "skipped"
        row.version += 1


def _message_part(session, task, source_message_id):
    return session.scalar(select(CloneMessagePart).where(
        CloneMessagePart.task_id == task.id,
        CloneMessagePart.epoch == task.task_lifecycle_epoch,
        CloneMessagePart.source_message_id == source_message_id,
    ).order_by(CloneMessagePart.part_index).limit(1))


def _sender_execution(session, route_id, part):
    row = session.scalar(select(CloneTargetExecutionSnapshot).where(
        CloneTargetExecutionSnapshot.route_snapshot_id == route_id,
        CloneTargetExecutionSnapshot.execution_role == "sender",
        CloneTargetExecutionSnapshot.authorization_id == part.authorization_id,
        CloneTargetExecutionSnapshot.session_generation == part.session_generation,
        CloneTargetExecutionSnapshot.execution_binding_hash == part.execution_binding_hash,
    ))
    if row is None:
        raise RuntimeError("group_clone_original_sender_execution_snapshot_missing")
    return row


def _topic_map(session, task, *, event, source_top):
    row = session.scalar(select(CloneTopicMap).where(
        CloneTopicMap.task_id == task.id,
        CloneTopicMap.epoch == task.task_lifecycle_epoch,
        CloneTopicMap.source_peer_type == event.source_peer_type,
        CloneTopicMap.source_peer_id == event.source_peer_id,
        CloneTopicMap.source_top_message_id == source_top,
    ))
    if row:
        return row
    row = CloneTopicMap(
        task_id=task.id,
        epoch=task.task_lifecycle_epoch,
        source_peer_type=event.source_peer_type,
        source_peer_id=event.source_peer_id,
        source_top_message_id=source_top,
        state="placeholder",
    )
    session.add(row)
    session.flush()
    return row


def _fresh_source_topic(session, task, event, *, source_top):
    stream = session.scalar(select(CloneSourceStreamState).where(
        CloneSourceStreamState.task_id == task.id,
        CloneSourceStreamState.task_lifecycle_epoch == task.task_lifecycle_epoch,
    ))
    authorization = session.get(TgAccountAuthorization, stream.authorization_id) if stream else None
    if authorization is None:
        return None
    try:
        snapshot = gateway.fetch_raw_forum_topic(
            event.source_peer_id,
            source_top,
            session_ciphertext=authorization.session_ciphertext,
            credentials=credentials_for_authorization(session, authorization),
        )
    except Exception:
        return None
    if int(snapshot.get("topic_id") or 0) != source_top or not str(snapshot.get("title") or "").strip():
        return None
    return snapshot


def _topic_title(event, *, default="Topic") -> str:
    snapshot = event.poll_snapshot or {}
    return str(snapshot.get("title") or event.content or default).strip()


def _wait_for_original(obligation, original, code):
    if original and original.state in {"executing", "unknown_after_send", "action_bound"}:
        return _wait(obligation, code)
    obligation.state = "waiting_manual_review"
    obligation.error_code = f"{code}_terminal"
    return False


def _wait(obligation, code):
    obligation.state = "waiting_dependency"
    obligation.error_code = code
    return False


def _hash(value) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


__all__ = ["materialize_lifecycle_event", "materialize_topic_bootstrap"]

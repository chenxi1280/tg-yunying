from __future__ import annotations

import hashlib
import json

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models import TgAccountAuthorization
from app.models.group_clone import TelegramGatewayMutationIdentity
from app.schemas.task_center import GroupCloneConfig

from .group_clone_content import clone_content_entities, sanitize_clone_content
from .group_clone_identity import derive_deterministic_random_id
from .payloads import GroupCloneMediaItem

GROUP_CLONE_CONTRACT = "v2_group_clone"
SUPPORTED_CLONE_MEDIA_TYPES = frozenset({
    "text", "photo", "video", "video_note", "document",
    "audio", "voice", "animation", "sticker", "poll",
})
MAX_ALBUM_ITEMS = 10


def admit_media_events(events, obligation, *, policy):
    if events is None or not events:
        return events
    if len(events) > MAX_ALBUM_ITEMS:
        _block_media(obligation, "album_batch_limit_exceeded")
        return None
    unsupported = sorted({
        item.media_type or "unknown" for item in events
        if (item.media_type or "unknown") not in SUPPORTED_CLONE_MEDIA_TYPES
    })
    if unsupported:
        _block_media(
            obligation, f"unsupported_media:{','.join(unsupported)}:{policy}",
        )
        return None
    return events


def _block_media(obligation, code) -> None:
    obligation.state = "waiting_manual_review"
    obligation.error_code = code


def materialize_media_items(
    session, task, *, route, execution, obligation, events, primary_content,
):
    result = []
    mutation_kind = "sendMultiMedia" if len(events) > 1 else "sendMedia"
    contents = _media_contents(
        session, task, events=events, primary_content=primary_content,
    )
    for index, (event, content) in enumerate(zip(events, contents, strict=True)):
        identity = allocate_send_identity(
            session, task, route=route, execution=execution, event=event,
            obligation=obligation, content=content or "",
            mutation_kind=mutation_kind, part_index=index,
        )
        result.append(GroupCloneMediaItem(
            gateway_mutation_identity_id=identity.id,
            source_message_id=event.source_message_id,
            media_type=event.media_type or "unknown",
            content=content or "",
            entities=clone_content_entities(event, content),
            poll_snapshot=event.poll_snapshot,
            random_id=identity.random_id,
        ))
    return result


def _media_contents(session, task, *, events, primary_content):
    return [
        primary_content if index == 0 else (
            sanitize_clone_content(
                session, task, config=_event_config(event), event=event,
            ) or ""
        )
        for index, event in enumerate(events)
    ]


def allocate_send_identity(
    session, task, *, route, execution, event, obligation, content,
    mutation_kind="sendMessage", part_index=0,
):
    authorization = session.get(TgAccountAuthorization, execution.authorization_id)
    account_peer = str(authorization.telegram_user_id_digest or "") if authorization else ""
    if not account_peer:
        raise RuntimeError("canonical_telegram_account_peer_id_unproven")
    fingerprint = _fingerprint(event, content)
    existing = _identity(
        session, obligation, mutation_kind=mutation_kind, part_index=part_index,
    )
    if existing:
        if existing.request_fingerprint != fingerprint:
            raise RuntimeError("group_clone_mutation_identity_fingerprint_conflict")
        return existing
    return _insert_identity(
        session, task, route=route, execution=execution, event=event,
        obligation=obligation, fingerprint=fingerprint,
        mutation_kind=mutation_kind, part_index=part_index,
        account_peer=account_peer,
    )


def _insert_identity(
    session, task, *, route, execution, event, obligation, fingerprint,
    mutation_kind, part_index, account_peer,
):
    nonce = 0
    while True:
        random_id = derive_deterministic_random_id(
            GROUP_CLONE_CONTRACT, task.tenant_id, task_id=task.id,
            epoch=task.task_lifecycle_epoch, obligation_id=obligation.id,
            mutation_kind=mutation_kind, part_index=part_index,
            collision_nonce=nonce,
        )
        identity = TelegramGatewayMutationIdentity(
            tenant_id=task.tenant_id, task_id=task.id,
            epoch=task.task_lifecycle_epoch, obligation_id=obligation.id,
            mutation_kind=mutation_kind, execution_role="sender",
            account_id=execution.account_id, telegram_account_peer_id=account_peer,
            authorization_id=execution.authorization_id,
            session_generation=execution.session_generation,
            target_peer_type=route.target_peer_type,
            target_peer_id=route.target_peer_id, random_id=random_id,
            collision_nonce=nonce, part_index=part_index,
            request_fingerprint=fingerprint,
        )
        try:
            with session.begin_nested():
                session.add(identity)
                session.flush()
            return identity
        except IntegrityError:
            nonce += 1


def _identity(session, obligation, *, mutation_kind, part_index):
    return session.scalar(select(TelegramGatewayMutationIdentity).where(
        TelegramGatewayMutationIdentity.task_id == obligation.task_id,
        TelegramGatewayMutationIdentity.epoch == obligation.epoch,
        TelegramGatewayMutationIdentity.obligation_id == obligation.id,
        TelegramGatewayMutationIdentity.materialization_version == obligation.materialization_version,
        TelegramGatewayMutationIdentity.mutation_kind == mutation_kind,
        TelegramGatewayMutationIdentity.part_index == part_index,
    ))


def _event_config(event) -> GroupCloneConfig:
    snapshot = dict(event.config_snapshot or {})
    if not snapshot:
        raise RuntimeError("group_clone_event_config_snapshot_missing")
    return GroupCloneConfig.model_validate(snapshot)


def _fingerprint(event, content) -> str:
    raw = json.dumps({
        "content": content,
        "entities": clone_content_entities(event, content),
        "media_type": event.media_type,
        "poll_snapshot": event.poll_snapshot,
        "source_message_id": event.source_message_id,
    }, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


__all__ = [
    "admit_media_events", "allocate_send_identity", "materialize_media_items",
]

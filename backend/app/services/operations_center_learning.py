from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ChannelMessage, OperationTarget, TgGroup
from app.services._common import audit
from app.services.group_listeners import collect_group_context
from app.services.operations import sync_channel_message_comments, sync_operation_target_messages
from app.services.target_learning import (
    GROUP_CHAT_SCENE,
    get_learning_profile_payload,
    list_learning_samples_payload,
    operation_target_for_group,
)


def listener_learning_profile(session: Session, tenant_id: int, object_type: str, object_id: int) -> dict:
    target = _target_for_learning_object(session, tenant_id, object_type, object_id)
    return get_learning_profile_payload(session, tenant_id, target.id)


def listener_learning_samples(session: Session, tenant_id: int, object_type: str, object_id: int, filters: dict | None = None) -> dict:
    target = _target_for_learning_object(session, tenant_id, object_type, object_id)
    return list_learning_samples_payload(session, tenant_id, target.id, filters)


def refresh_listener_learning(session: Session, tenant_id: int, object_type: str, object_id: int, actor: str) -> dict:
    if object_type == "group":
        return _refresh_group_learning(session, tenant_id, object_id, actor)
    if object_type == "channel":
        return _refresh_channel_comment_learning(session, tenant_id, object_id, actor)
    raise ValueError("监听对象类型不支持")


def _refresh_group_learning(session: Session, tenant_id: int, group_id: int, actor: str) -> dict:
    group = session.get(TgGroup, group_id)
    if not group or group.tenant_id != tenant_id:
        raise ValueError("监听对象不存在")
    inserted = collect_group_context(session, group, create_source_media=False, learning_scene=GROUP_CHAT_SCENE)
    audit(session, tenant_id=tenant_id, actor=actor, action="刷新监听学习", target_type="group", target_id=str(group_id), detail=f"inserted={inserted}")
    session.commit()
    target = operation_target_for_group(session, group)
    return {"inserted": inserted, "learning_profile": get_learning_profile_payload(session, tenant_id, target.id) if target else {}}


def _refresh_channel_comment_learning(session: Session, tenant_id: int, target_id: int, actor: str) -> dict:
    message_result = sync_operation_target_messages(session, tenant_id, target_id, actor)
    _raise_sync_error(message_result)
    inserted = 0
    for message in _recent_channel_messages(session, tenant_id, target_id):
        result = sync_channel_message_comments(session, tenant_id, message.id, actor)
        _raise_sync_error(result)
        inserted += int(result.get("inserted") or 0)
    return {"inserted": inserted, "learning_profile": get_learning_profile_payload(session, tenant_id, target_id)}


def _raise_sync_error(result: dict) -> None:
    detail = result.get("detail") if isinstance(result.get("detail"), dict) else {}
    sync_error = str(result.get("sync_error") or detail.get("sync_error") or "").strip()
    if sync_error:
        raise ValueError(sync_error)


def _recent_channel_messages(session: Session, tenant_id: int, target_id: int) -> list[ChannelMessage]:
    return list(
        session.scalars(
            select(ChannelMessage)
            .where(ChannelMessage.tenant_id == tenant_id, ChannelMessage.channel_target_id == target_id)
            .order_by(ChannelMessage.published_at.desc().nullslast(), ChannelMessage.created_at.desc())
            .limit(20)
        )
    )


def _target_for_learning_object(session: Session, tenant_id: int, object_type: str, object_id: int) -> OperationTarget:
    if object_type == "channel":
        target = session.get(OperationTarget, object_id)
        if target and target.tenant_id == tenant_id and target.target_type == "channel":
            return target
    if object_type == "group":
        target = _group_learning_target(session, tenant_id, object_id)
        if target:
            return target
    raise ValueError("监听学习目标不存在")


def _group_learning_target(session: Session, tenant_id: int, group_id: int) -> OperationTarget | None:
    group = session.get(TgGroup, group_id)
    if not group or group.tenant_id != tenant_id:
        return None
    return operation_target_for_group(session, group)


__all__ = ["listener_learning_profile", "listener_learning_samples", "refresh_listener_learning"]

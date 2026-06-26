from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import ChannelMessage, OperationTarget, TenantLearningSource, TgAccount, TgGroup, TgGroupAccount
from app.models.enums import AccountStatus
from app.services._common import _now, audit


def list_source_candidates(session: Session, tenant_id: int) -> dict[str, Any]:
    groups = _candidate_groups(session, tenant_id)
    targets_by_peer = _group_targets_by_peer(session, tenant_id)
    links_by_group = _group_links_by_group(session, tenant_id, [group.id for group in groups])
    group_items = [
        _group_candidate_payload(group, targets_by_peer.get(group.tg_peer_id), links_by_group.get(group.id, []))
        for group in groups
    ]
    channel_items = _channel_candidate_payloads(session, tenant_id)
    items = [*group_items, *channel_items]
    return {"items": items, "total": len(items)}


def list_sources(session: Session, tenant_id: int) -> dict[str, Any]:
    sources = session.scalars(
        select(TenantLearningSource)
        .where(TenantLearningSource.tenant_id == tenant_id)
        .order_by(TenantLearningSource.selected_at.desc())
    ).all()
    return {"items": [_source_payload(source, session.get(OperationTarget, source.target_id)) for source in sources], "total": len(sources)}


def update_sources(session: Session, tenant_id: int, payload: dict[str, Any], *, actor: str, reason: str) -> dict[str, Any]:
    if not reason.strip():
        raise ValueError("请填写学习来源变更原因")
    source_items = list(payload.get("sources") or [])
    resolved_sources = [_resolve_source_target(session, tenant_id, item) for item in source_items]
    target_ids = [target.id for item, target, _listener_ids in resolved_sources]
    existing = _existing_sources_by_target(session, tenant_id)
    for item, target, listener_ids in resolved_sources:
        source = existing.get(target.id) or TenantLearningSource(tenant_id=tenant_id, target_id=target.id)
        _apply_source_selection(source, item, target, listener_ids, actor)
        if source not in session:
            session.add(source)
    for target_id, source in existing.items():
        if target_id not in target_ids:
            source.is_enabled = False
            source.source_status = "disabled"
    audit(session, tenant_id=tenant_id, actor=actor, action="配置全站目标画像学习来源", target_type="target_profile", target_id=str(tenant_id), detail=reason.strip())
    session.flush()
    return list_sources(session, tenant_id)


def _candidate_groups(session: Session, tenant_id: int) -> list[TgGroup]:
    return list(
        session.scalars(
            select(TgGroup)
            .where(TgGroup.tenant_id == tenant_id, TgGroup.group_type != "channel")
            .order_by(TgGroup.listener_enabled.desc(), TgGroup.id.desc())
        )
    )


def _group_targets_by_peer(session: Session, tenant_id: int) -> dict[str, OperationTarget]:
    targets = session.scalars(
        select(OperationTarget).where(OperationTarget.tenant_id == tenant_id, OperationTarget.target_type == "group")
    ).all()
    return {target.tg_peer_id: target for target in targets}


def _channel_candidate_payloads(session: Session, tenant_id: int) -> list[dict[str, Any]]:
    account_ids = _active_account_ids(session, tenant_id)
    recent_by_target = _recent_channel_messages_by_target(session, tenant_id)
    targets = session.scalars(
        select(OperationTarget)
        .where(OperationTarget.tenant_id == tenant_id, OperationTarget.target_type == "channel")
        .order_by(OperationTarget.last_sync_at.desc().nullslast(), OperationTarget.id.desc())
    ).all()
    return [_channel_candidate_payload(target, account_ids, recent_by_target.get(target.id)) for target in targets]


def _active_account_ids(session: Session, tenant_id: int) -> list[int]:
    return list(
        session.scalars(
            select(TgAccount.id)
            .where(
                TgAccount.tenant_id == tenant_id,
                TgAccount.deleted_at.is_(None),
                TgAccount.status == AccountStatus.ACTIVE.value,
            )
            .order_by(TgAccount.id.asc())
            .limit(200)
        )
    )


def _recent_channel_messages_by_target(session: Session, tenant_id: int) -> dict[int, datetime]:
    rows = session.execute(
        select(ChannelMessage.channel_target_id, func.max(ChannelMessage.published_at))
        .where(ChannelMessage.tenant_id == tenant_id)
        .group_by(ChannelMessage.channel_target_id)
    ).all()
    return {int(target_id): published_at for target_id, published_at in rows if published_at}


def _channel_candidate_payload(target: OperationTarget, account_ids: list[int], recent_message_at: datetime | None) -> dict[str, Any]:
    can_listen = bool(account_ids)
    return {
        "source_key": f"target:{target.id}",
        "group_id": None,
        "target_id": target.id,
        "target_type": "channel",
        "title": target.title,
        "tg_peer_id": target.tg_peer_id,
        "can_listen": can_listen,
        "listener_account_ids": account_ids,
        "recent_message_at": _iso(recent_message_at or target.last_sync_at),
        "associated_task_types": ["channel_comment", "discussion_reply"],
        "recommended": can_listen,
        "recommend_reason": "频道评论区可采集" if can_listen else "",
        "cannot_auto_sync_reason": "" if can_listen else "no_listener_account",
    }


def _group_links_by_group(session: Session, tenant_id: int, group_ids: list[int]) -> dict[int, list[Any]]:
    if not group_ids:
        return {}
    links: dict[int, list[Any]] = {group_id: [] for group_id in group_ids}
    stmt = (
        select(TgGroupAccount)
        .join(TgAccount, TgAccount.id == TgGroupAccount.account_id)
        .where(
            TgGroupAccount.tenant_id == tenant_id,
            TgGroupAccount.group_id.in_(group_ids),
            TgGroupAccount.is_listener.is_(True),
            TgAccount.tenant_id == tenant_id,
            TgAccount.deleted_at.is_(None),
            TgAccount.status == AccountStatus.ACTIVE.value,
        )
    )
    for link in session.scalars(stmt):
        links.setdefault(link.group_id, []).append(link)
    return links


def _group_candidate_payload(group: TgGroup, target: OperationTarget | None, links: list[Any]) -> dict[str, Any]:
    listener_ids = [link.account_id for link in links if link.is_listener]
    can_listen = bool(group.listener_enabled or listener_ids)
    return {
        "source_key": f"group:{group.id}",
        "group_id": group.id,
        "target_id": target.id if target else None,
        "target_type": "group",
        "title": group.title,
        "tg_peer_id": group.tg_peer_id,
        "can_listen": can_listen,
        "listener_account_ids": listener_ids,
        "recent_message_at": _iso(group.listener_last_polled_at),
        "associated_task_types": [],
        "recommended": can_listen,
        "recommend_reason": "可监听群聊" if can_listen else "",
        "cannot_auto_sync_reason": "" if can_listen else "no_listener_account",
    }


def _resolve_source_target(session: Session, tenant_id: int, item: dict[str, Any]) -> tuple[dict[str, Any], OperationTarget, list[int]]:
    if item.get("group_id"):
        group = session.get(TgGroup, int(item["group_id"]))
        if not group or group.tenant_id != tenant_id or group.group_type == "channel":
            raise ValueError("学习来源群聊不存在")
        links_by_group = _group_links_by_group(session, tenant_id, [group.id])
        target = _ensure_group_target(session, group)
        listener_ids = [link.account_id for link in links_by_group.get(group.id, []) if link.is_listener]
        return item, target, _validated_listener_ids(item, listener_ids)
    if not item.get("target_id"):
        raise ValueError("学习来源目标不存在")
    target = session.get(OperationTarget, int(item["target_id"]))
    if not target or target.tenant_id != tenant_id:
        raise ValueError("学习来源目标不存在")
    listener_ids = _listener_ids_for_target(session, tenant_id, target)
    return item, target, _validated_listener_ids(item, listener_ids)


def _listener_ids_for_target(session: Session, tenant_id: int, target: OperationTarget) -> list[int]:
    if target.target_type == "channel":
        return _active_account_ids(session, tenant_id)
    group = session.scalar(select(TgGroup).where(TgGroup.tenant_id == tenant_id, TgGroup.tg_peer_id == target.tg_peer_id))
    if not group:
        return []
    links_by_group = _group_links_by_group(session, tenant_id, [group.id])
    return [link.account_id for link in links_by_group.get(group.id, [])]


def _validated_listener_ids(item: dict[str, Any], allowed_ids: list[int]) -> list[int]:
    requested_ids = _requested_listener_ids(item)
    if not requested_ids:
        return allowed_ids
    allowed = set(allowed_ids)
    if any(account_id not in allowed for account_id in requested_ids):
        raise ValueError("监听账号不属于该学习来源")
    return requested_ids


def _requested_listener_ids(item: dict[str, Any]) -> list[int]:
    try:
        return list(dict.fromkeys(int(account_id) for account_id in item.get("listener_account_ids") or []))
    except (TypeError, ValueError) as exc:
        raise ValueError("监听账号不属于该学习来源") from exc


def _ensure_group_target(session: Session, group: TgGroup) -> OperationTarget:
    target = session.scalar(select(OperationTarget).where(OperationTarget.tenant_id == group.tenant_id, OperationTarget.tg_peer_id == group.tg_peer_id))
    if not target:
        target = OperationTarget(tenant_id=group.tenant_id, tg_peer_id=group.tg_peer_id)
        session.add(target)
    target.target_type = "group"
    target.title = group.title
    target.member_count = group.member_count
    target.can_send = group.can_send
    target.auth_status = group.auth_status
    target.updated_at = _now()
    session.flush()
    return target


def _existing_sources_by_target(session: Session, tenant_id: int) -> dict[int, TenantLearningSource]:
    sources = session.scalars(select(TenantLearningSource).where(TenantLearningSource.tenant_id == tenant_id)).all()
    return {source.target_id: source for source in sources}


def _apply_source_selection(source: TenantLearningSource, item: dict[str, Any], target: OperationTarget, listener_ids: list[int], actor: str) -> None:
    source.source_kind = target.target_type
    source.is_enabled = bool(item.get("is_enabled", True))
    source.auto_sync_enabled = bool(item.get("auto_sync_enabled", True))
    source.listener_account_ids = list(item.get("listener_account_ids") or listener_ids)
    source.source_status = "active" if source.is_enabled else "disabled"
    source.last_failure_detail = str(item.get("last_failure_detail") or "")
    source.selected_by = actor


def _source_payload(source: TenantLearningSource, target: OperationTarget | None) -> dict[str, Any]:
    return {
        "id": source.id,
        "target_id": source.target_id,
        "target_title": target.title if target else "",
        "target_type": target.target_type if target else source.source_kind,
        "source_kind": source.source_kind,
        "is_enabled": source.is_enabled,
        "auto_sync_enabled": source.auto_sync_enabled,
        "source_status": source.source_status,
        "listener_account_ids": source.listener_account_ids or [],
        "last_sync_at": _iso(source.last_sync_at),
        "last_history_pull_at": _iso(source.last_history_pull_at),
        "last_failure_detail": source.last_failure_detail,
    }


def _iso(value: Any) -> str | None:
    return value.isoformat() if value else None


__all__ = ["list_source_candidates", "list_sources", "update_sources"]

from __future__ import annotations

import hashlib
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import AccountStatus, Action, SourceMediaAsset, TgAccount
from app.services._common import _now, gateway

SOURCE_MEDIA_READY = "ready"
SOURCE_MEDIA_PENDING = "pending_cache"
SOURCE_MEDIA_FLOOD_WAIT = "cache_flood_wait"
SOURCE_MEDIA_FAILED = "cache_failed"
SOURCE_MEDIA_UNRECOVERABLE = "unrecoverable"
WAITING_MATERIAL_CACHE = "waiting_cache"
DEFAULT_WAIT_LIMIT = 1000
DEFAULT_WAIT_MINUTES = 30


def source_media_fingerprint(*parts: object) -> str:
    raw = "\n".join(str(part or "") for part in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def ensure_source_media_asset(
    session: Session,
    *,
    tenant_id: int,
    source_group_id: int | None,
    listener_account_id: int | None,
    source_peer_id: str,
    source_message_id: str,
    source_media_group_id: str = "",
    media_group_index: int = 0,
    media_group_total: int = 1,
    media_type: str = "photo",
    caption: str = "",
    media_fingerprint: str = "",
    album_caption_policy: str = "per_item",
    cache_status: str = SOURCE_MEDIA_PENDING,
) -> SourceMediaAsset:
    fingerprint = media_fingerprint or source_media_fingerprint(
        source_peer_id,
        source_message_id,
        source_media_group_id,
        media_group_index,
        media_type,
        caption,
    )
    existing = session.scalar(
        select(SourceMediaAsset).where(
            SourceMediaAsset.tenant_id == tenant_id,
            SourceMediaAsset.source_group_id == source_group_id,
            SourceMediaAsset.source_message_id == source_message_id,
            SourceMediaAsset.source_media_group_id == source_media_group_id,
            SourceMediaAsset.media_group_index == media_group_index,
        )
    )
    if existing:
        existing.listener_account_id = listener_account_id or existing.listener_account_id
        existing.media_group_total = max(existing.media_group_total or 1, media_group_total or 1)
        existing.media_type = media_type or existing.media_type
        existing.caption = caption or existing.caption
        existing.media_fingerprint = existing.media_fingerprint or fingerprint
        existing.updated_at = _now()
        return existing
    asset = SourceMediaAsset(
        tenant_id=tenant_id,
        source_group_id=source_group_id,
        listener_account_id=listener_account_id,
        source_peer_id=source_peer_id,
        source_message_id=source_message_id,
        source_media_group_id=source_media_group_id,
        media_group_index=media_group_index,
        media_group_total=media_group_total or 1,
        album_caption_policy=album_caption_policy,
        media_type=media_type or "photo",
        caption=caption,
        media_fingerprint=fingerprint,
        cache_status=cache_status,
        expires_at=_now() + timedelta(days=10),
    )
    session.add(asset)
    session.flush()
    return asset


def waiting_queue_full(session: Session, tenant_id: int, limit: int = DEFAULT_WAIT_LIMIT) -> bool:
    waiting = session.scalar(select(Action.id).where(Action.tenant_id == tenant_id, Action.status == WAITING_MATERIAL_CACHE).limit(limit + 1))
    if not waiting:
        return False
    count = session.query(Action).filter(Action.tenant_id == tenant_id, Action.status == WAITING_MATERIAL_CACHE).count()
    return count >= limit


def register_action_waiting_for_source_media(
    session: Session,
    action: Action,
    asset_ids: list[str],
    *,
    wait_minutes: int = DEFAULT_WAIT_MINUTES,
    queue_limit: int = DEFAULT_WAIT_LIMIT,
) -> bool:
    if waiting_queue_full(session, action.tenant_id, queue_limit):
        action.status = "skipped"
        action.result = {
            "success": False,
            "error_code": "material_cache_wait_queue_full",
            "error_message": "等待源媒体缓存队列已满，放弃本次媒体缓存",
            "auto_check": "跳过",
            "validation_stage": "source_media_cache",
        }
        action.executed_at = _now()
        for asset in session.scalars(select(SourceMediaAsset).where(SourceMediaAsset.id.in_(asset_ids))):
            asset.cache_status = SOURCE_MEDIA_FAILED
            asset.failure_reason = "material_cache_wait_queue_full"
            asset.updated_at = _now()
        return False
    payload = dict(action.payload or {})
    payload["waiting_source_media_asset_ids"] = list(dict.fromkeys(asset_ids))
    assets = list(session.scalars(select(SourceMediaAsset).where(SourceMediaAsset.id.in_(asset_ids))))
    payload["waiting_source_media_versions"] = {asset.id: asset.cache_version for asset in assets}
    payload["material_cache_wait_until"] = (_now() + timedelta(minutes=max(1, wait_minutes))).isoformat()
    action.payload = payload
    action.status = WAITING_MATERIAL_CACHE
    action.result = {
        "success": False,
        "error_code": "waiting_material_cache",
        "error_message": "等待源媒体缓存完成",
        "auto_check": "等待",
        "validation_stage": "source_media_cache",
    }
    return True


def ready_media_segments(session: Session, asset_ids: list[str], expected_versions: dict[str, int] | None = None) -> list[dict[str, Any]]:
    assets = list(session.scalars(select(SourceMediaAsset).where(SourceMediaAsset.id.in_(asset_ids))))
    asset_by_id = {asset.id: asset for asset in assets}
    ordered = sorted(
        (asset_by_id[asset_id] for asset_id in asset_ids if asset_id in asset_by_id),
        key=lambda asset: (asset.source_media_group_id or asset.source_message_id or "", asset.media_group_index, asset.created_at),
    )
    segments: list[dict[str, Any]] = []
    for asset in ordered:
        if asset.cache_status != SOURCE_MEDIA_READY:
            continue
        if expected_versions and asset.cache_version < expected_versions.get(asset.id, 0):
            continue
        if not asset.cache_peer_id or not asset.cache_message_id:
            continue
        segments.append(
            {
                "segment_type": _segment_type(asset.media_type),
                "source": f"tg-cache://{asset.cache_peer_id}/{asset.cache_message_id}",
                "caption": asset.caption,
                "source_media_asset_id": asset.id,
                "media_group_index": asset.media_group_index,
            }
        )
    return segments


def source_media_cached_event(
    session: Session,
    *,
    source_media_asset_id: str,
    cache_peer_id: str,
    cache_message_id: str,
    cache_version: int | None = None,
) -> int:
    asset = session.get(SourceMediaAsset, source_media_asset_id)
    if not asset:
        return 0
    incoming_version = cache_version if cache_version is not None else asset.cache_version + 1
    if incoming_version < asset.cache_version:
        asset.failure_reason = "stale_source_media_cached_event"
        asset.updated_at = _now()
        return 0
    asset.cache_peer_id = cache_peer_id
    asset.cache_message_id = cache_message_id
    asset.cache_status = SOURCE_MEDIA_READY
    asset.cache_version = incoming_version
    asset.failure_reason = ""
    asset.last_cached_at = _now()
    asset.updated_at = _now()
    return wake_waiting_actions_for_source_media(session, asset.tenant_id)


def drain_source_media_cache(session_factory, limit: int = 20) -> int:
    cache_peer_id = get_settings().source_media_cache_peer_id
    if not cache_peer_id:
        with session_factory() as session:
            pending = list(
                session.scalars(
                    select(SourceMediaAsset)
                    .where(SourceMediaAsset.cache_status.in_([SOURCE_MEDIA_PENDING, SOURCE_MEDIA_FLOOD_WAIT]))
                    .order_by(SourceMediaAsset.created_at.asc())
                    .limit(max(1, limit))
                )
            )
            for asset in pending:
                asset.failure_reason = "cache_peer_unavailable"
                asset.updated_at = _now()
            if pending:
                session.commit()
        return 0
    processed = 0
    with session_factory() as session:
        assets = list(
            session.scalars(
                select(SourceMediaAsset)
                .where(
                    SourceMediaAsset.cache_status.in_([SOURCE_MEDIA_PENDING, SOURCE_MEDIA_FLOOD_WAIT]),
                    (SourceMediaAsset.next_retry_at.is_(None)) | (SourceMediaAsset.next_retry_at <= _now()),
                )
                .order_by(SourceMediaAsset.created_at.asc())
                .limit(max(1, limit))
            )
        )
        for asset in assets:
            account = session.get(TgAccount, asset.listener_account_id) if asset.listener_account_id else None
            if not account or account.deleted_at is not None or account.status != AccountStatus.ACTIVE.value:
                _mark_asset_failed(asset, "cache_account_unavailable")
                processed += 1
                continue
            try:
                from app.services.developer_apps import credentials_for_account

                credentials = credentials_for_account(session, account)
                result = gateway.cache_source_media(
                    account.id,
                    asset.source_peer_id,
                    asset.source_message_id,
                    cache_peer_id,
                    account.session_ciphertext,
                    credentials,
                )
            except Exception as exc:  # noqa: BLE001 - worker keeps draining.
                result = type("_SourceMediaResult", (), {"ok": False, "failure_type": "source_media_cache_failed", "detail": str(exc), "remote_message_id": ""})()
            if result.ok and result.remote_message_id:
                source_media_cached_event(
                    session,
                    source_media_asset_id=asset.id,
                    cache_peer_id=cache_peer_id,
                    cache_message_id=str(result.remote_message_id),
                    cache_version=asset.cache_version,
                )
            else:
                failure_type = str(result.failure_type or "source_media_cache_failed")
                if failure_type == "FloodWait":
                    asset.cache_status = SOURCE_MEDIA_FLOOD_WAIT
                    asset.retry_count += 1
                    asset.next_retry_at = _now() + timedelta(minutes=min(30, max(1, asset.retry_count * 2)))
                    asset.failure_reason = failure_type
                elif failure_type in {"source_media_unrecoverable", "cache_peer_unavailable"}:
                    asset.cache_status = SOURCE_MEDIA_UNRECOVERABLE
                    asset.failure_reason = failure_type
                else:
                    _mark_asset_failed(asset, failure_type)
            asset.updated_at = _now()
            processed += 1
        if processed:
            session.commit()
    return processed


def wake_waiting_actions_for_source_media(session: Session, tenant_id: int, *, limit: int = 200) -> int:
    woke = 0
    actions = list(
        session.scalars(
            select(Action)
            .where(Action.tenant_id == tenant_id, Action.status == WAITING_MATERIAL_CACHE)
            .order_by(Action.scheduled_at.asc(), Action.created_at.asc())
            .limit(limit)
        )
    )
    for action in actions:
        asset_ids = [str(item) for item in (action.payload or {}).get("waiting_source_media_asset_ids") or [] if str(item)]
        expected_versions = {str(key): int(value) for key, value in ((action.payload or {}).get("waiting_source_media_versions") or {}).items()}
        if not asset_ids:
            continue
        assets = list(session.scalars(select(SourceMediaAsset).where(SourceMediaAsset.id.in_(asset_ids))))
        if not assets:
            _skip_waiting_action(action, "source_media_unrecoverable", "源媒体缓存记录不存在")
            continue
        ready_ids = {
            asset.id
            for asset in assets
            if asset.cache_status == SOURCE_MEDIA_READY
            and asset.cache_peer_id
            and asset.cache_message_id
            and asset.cache_version >= expected_versions.get(asset.id, 0)
        }
        failed_assets = [asset for asset in assets if asset.cache_status in {SOURCE_MEDIA_FAILED, SOURCE_MEDIA_UNRECOVERABLE}]
        segments = ready_media_segments(session, asset_ids, expected_versions)
        finished_count = len(ready_ids) + len(failed_assets)
        if finished_count < len(asset_ids):
            continue
        if not segments:
            _skip_waiting_action(action, "source_media_cache_failed", "源媒体缓存失败，按规则跳过媒体")
            continue
        payload = dict(action.payload or {})
        payload["media_segments"] = segments
        payload["album_segment_results"] = [
            {
                "source_media_asset_id": asset.id,
                "media_group_index": asset.media_group_index,
                "status": "ready" if asset.id in ready_ids else "album_segment_failed",
                "reason": "" if asset.id in ready_ids else (asset.failure_reason or asset.cache_status),
            }
            for asset in sorted(assets, key=lambda item: (item.source_media_group_id or item.source_message_id or "", item.media_group_index, item.created_at))
        ]
        action.payload = payload
        action.status = "pending"
        action.result = {**(action.result or {}), "error_code": "", "error_message": "", "auto_check": "待发送", "validation_stage": "source_media_cached"}
        woke += 1
    return woke


def expire_waiting_source_media_actions(session: Session, tenant_id: int | None = None, *, limit: int = 200) -> int:
    now_value = _now()
    conditions = [Action.status == WAITING_MATERIAL_CACHE]
    if tenant_id is not None:
        conditions.append(Action.tenant_id == tenant_id)
    expired = 0
    for action in session.scalars(select(Action).where(*conditions).order_by(Action.scheduled_at.asc()).limit(limit)):
        wait_until = str((action.payload or {}).get("material_cache_wait_until") or "")
        if wait_until and wait_until > now_value.isoformat():
            continue
        payload = dict(action.payload or {})
        expected_versions = {str(key): int(value) for key, value in (payload.get("waiting_source_media_versions") or {}).items()}
        payload["media_segments"] = ready_media_segments(session, [str(item) for item in payload.get("waiting_source_media_asset_ids") or []], expected_versions)
        action.payload = payload
        if payload["media_segments"]:
            action.status = "pending"
            action.result = {**(action.result or {}), "error_code": "material_cache_wait_timeout", "error_message": "源媒体等待超时，剔除失败图后继续发送", "validation_stage": "source_media_cache"}
        else:
            _skip_waiting_action(action, "material_cache_wait_timeout", "源媒体等待超时且没有可发送媒体")
        expired += 1
    return expired


def _skip_waiting_action(action: Action, code: str, detail: str) -> None:
    action.status = "skipped"
    action.result = {"success": False, "error_code": code, "error_message": detail, "auto_check": "跳过", "validation_stage": "source_media_cache"}
    action.executed_at = _now()


def _mark_asset_failed(asset: SourceMediaAsset, reason: str) -> None:
    asset.cache_status = SOURCE_MEDIA_FAILED
    asset.failure_reason = reason
    asset.retry_count += 1
    asset.next_retry_at = None
    asset.updated_at = _now()


def _segment_type(media_type: str) -> str:
    normalized = (media_type or "").lower()
    if "sticker" in normalized:
        return "表情包"
    if "document" in normalized or "file" in normalized:
        return "文件"
    return "图片"


__all__ = [
    "SOURCE_MEDIA_FAILED",
    "SOURCE_MEDIA_FLOOD_WAIT",
    "SOURCE_MEDIA_PENDING",
    "SOURCE_MEDIA_READY",
    "SOURCE_MEDIA_UNRECOVERABLE",
    "WAITING_MATERIAL_CACHE",
    "ensure_source_media_asset",
    "drain_source_media_cache",
    "expire_waiting_source_media_actions",
    "ready_media_segments",
    "register_action_waiting_for_source_media",
    "source_media_cached_event",
    "source_media_fingerprint",
    "wake_waiting_actions_for_source_media",
]

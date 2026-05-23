from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from app.models import AccountStatus, Material, TgAccount
from app.services.ai_config import resolve_material_cache_peer_id
from app.services._common import _now, gateway
from app.services.material_ingestion import is_platform_temp_path, remove_platform_temp_file
from app.services.material_versions import record_material_tg_ref_version

MEDIA_MATERIAL_TYPES = {"图片", "表情包", "文件"}


def drain_material_cache(session_factory, limit: int = 20) -> int:
    processed = 0
    with session_factory() as session:
        materials = list(
            session.scalars(
                select(Material)
                .where(
                    Material.material_type.in_(MEDIA_MATERIAL_TYPES),
                    Material.cache_ready_status.in_(["not_cached", "cache_failed", "refreshing", "flood_wait"]),
                    Material.content != "",
                )
                .order_by(Material.id.asc())
                .limit(max(1, limit))
            )
        )
        for material in materials:
            cache_peer_id = resolve_material_cache_peer_id(session, material.tenant_id)
            if not cache_peer_id:
                _mark_material_cache_failed(material, "cache_peer_unavailable")
                processed += 1
                continue
            if material.cache_ready_status == "flood_wait" and material.last_cache_flood_wait_until and material.last_cache_flood_wait_until > _now():
                continue
            account = _cache_account(session, material)
            if not account:
                _mark_material_cache_failed(material, "cache_account_unavailable")
                processed += 1
                continue
            try:
                from app.services.developer_apps import credentials_for_account

                credentials = credentials_for_account(session, account)
                material.cache_ready_status = "refreshing"
                result = gateway.cache_material_source(
                    account.id,
                    material.content,
                    cache_peer_id,
                    material.caption or "",
                    account.session_ciphertext,
                    credentials,
                )
            except Exception as exc:  # noqa: BLE001 - worker keeps draining.
                result = type("_MaterialCacheResult", (), {"ok": False, "failure_type": "cache_failed", "detail": str(exc), "remote_message_id": ""})()
            if result.ok and result.remote_message_id:
                source_was_temp = is_platform_temp_path(material.content)
                remove_platform_temp_file(material.content)
                material.tg_cache_account_id = account.id
                material.tg_cache_peer_id = cache_peer_id
                material.tg_cache_message_id = str(result.remote_message_id)
                material.cache_ready_status = "ready"
                if source_was_temp:
                    material.content = ""
                material.tg_ref_version_id += 1
                material.last_cache_error = ""
                record_material_tg_ref_version(session, material, actor="material-cache-worker")
            else:
                failure_type = str(result.failure_type or "cache_failed")
                if failure_type == "FloodWait":
                    material.cache_ready_status = "flood_wait"
                    material.last_cache_flood_wait_until = _now() + timedelta(minutes=2)
                    material.last_cache_error = failure_type
                else:
                    _mark_material_cache_failed(material, failure_type)
            processed += 1
        if processed:
            session.commit()
    return processed


def _cache_account(session, material: Material) -> TgAccount | None:
    if material.tg_cache_account_id:
        account = session.get(TgAccount, material.tg_cache_account_id)
        if account and account.deleted_at is None and account.status == AccountStatus.ACTIVE.value:
            return account
    return session.scalar(
        select(TgAccount)
        .where(TgAccount.tenant_id == material.tenant_id, TgAccount.deleted_at.is_(None), TgAccount.status == AccountStatus.ACTIVE.value)
        .order_by(TgAccount.health_score.desc(), TgAccount.id.asc())
        .limit(1)
    )


def _mark_material_cache_failed(material: Material, reason: str) -> None:
    material.cache_ready_status = "cache_failed"
    material.last_cache_error = reason


__all__ = ["drain_material_cache"]

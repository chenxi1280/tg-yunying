from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from app.models import Material, TgAccount
from app.services.ai_config import cache_candidate_accounts, resolve_material_cache_account_id, resolve_material_cache_peer_id
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
            accounts = _cache_accounts(session, material)
            if not accounts:
                _mark_material_cache_failed(material, "cache_account_unavailable")
                processed += 1
                continue
            from app.services.developer_apps import credentials_for_account

            result = None
            account = None
            material.cache_ready_status = "refreshing"
            for candidate in accounts:
                try:
                    credentials = credentials_for_account(session, candidate)
                    result = gateway.cache_material_source(
                        candidate.id,
                        material.content,
                        cache_peer_id,
                        material.caption or "",
                        candidate.session_ciphertext,
                        credentials,
                    )
                except Exception as exc:  # noqa: BLE001 - try the next active account before marking the material failed.
                    result = type("_MaterialCacheResult", (), {"ok": False, "failure_type": "cache_failed", "detail": str(exc), "remote_message_id": ""})()
                if result.ok and result.remote_message_id:
                    account = candidate
                    break
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


def _cache_accounts(session, material: Material) -> list[TgAccount]:
    preferred_account_id = material.tg_cache_account_id or resolve_material_cache_account_id(session, material.tenant_id)
    return cache_candidate_accounts(session, material.tenant_id, preferred_account_id)


def _mark_material_cache_failed(material: Material, reason: str) -> None:
    material.cache_ready_status = "cache_failed"
    material.last_cache_error = reason


__all__ = ["drain_material_cache"]

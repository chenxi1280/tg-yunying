from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Material, MaterialAssetVersion, MaterialTgRefVersion
from app.services.material_ingestion import is_platform_temp_path


def record_material_asset_version(session: Session, material: Material, *, actor: str = "") -> MaterialAssetVersion:
    existing = session.scalar(
        select(MaterialAssetVersion).where(
            MaterialAssetVersion.material_id == material.id,
            MaterialAssetVersion.asset_version_id == material.asset_version_id,
        )
    )
    if existing:
        return existing
    row = MaterialAssetVersion(
        tenant_id=material.tenant_id,
        material_id=material.id,
        asset_version_id=material.asset_version_id,
        source_kind=material.source_kind,
        content="" if is_platform_temp_path(material.content or "") else material.content,
        asset_fingerprint=material.asset_fingerprint,
        file_name=material.file_name,
        mime_type=material.mime_type,
        file_size=material.file_size,
        width=material.width,
        height=material.height,
        caption=material.caption,
        created_by=actor,
    )
    session.add(row)
    session.flush()
    return row


def record_material_tg_ref_version(session: Session, material: Material, *, actor: str = "", failure_reason: str = "") -> MaterialTgRefVersion:
    existing = session.scalar(
        select(MaterialTgRefVersion).where(
            MaterialTgRefVersion.material_id == material.id,
            MaterialTgRefVersion.tg_ref_version_id == material.tg_ref_version_id,
        )
    )
    if existing:
        return existing
    row = MaterialTgRefVersion(
        tenant_id=material.tenant_id,
        material_id=material.id,
        asset_version_id=material.asset_version_id,
        tg_ref_version_id=material.tg_ref_version_id,
        cache_status=material.cache_ready_status,
        tg_cache_account_id=material.tg_cache_account_id,
        tg_cache_peer_id=material.tg_cache_peer_id,
        tg_cache_message_id=material.tg_cache_message_id,
        gateway_type=material.gateway_type,
        failure_reason=failure_reason or material.last_cache_error,
        created_by=actor,
    )
    session.add(row)
    session.flush()
    return row


def record_material_versions(session: Session, material: Material, *, actor: str = "", include_tg_ref: bool = True) -> None:
    record_material_asset_version(session, material, actor=actor)
    if include_tg_ref:
        record_material_tg_ref_version(session, material, actor=actor)


__all__ = ["record_material_asset_version", "record_material_tg_ref_version", "record_material_versions"]

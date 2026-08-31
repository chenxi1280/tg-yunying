from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    CommentFallbackSelection,
    Material,
    MaterialAssetVersion,
    MaterialGroup,
    MaterialTgRefVersion,
)


STATIC_IMAGE_MIME_TYPES = frozenset({"image/jpeg", "image/jpg", "image/png", "image/webp"})
STATIC_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")


def ready_image_assets(
    session: Session,
    tenant_id: int,
    group_id: int | None,
) -> list[tuple[Material, MaterialAssetVersion, MaterialTgRefVersion]]:
    if not group_id:
        return []
    group = session.get(MaterialGroup, int(group_id))
    if (
        not group
        or group.tenant_id != tenant_id
        or not group.is_active
        or group.membership_state != "ready"
    ):
        return []
    material_ids = sorted({int(item) for item in (group.material_ids or [])})
    if not material_ids:
        return []
    statement = (
        select(Material, MaterialAssetVersion, MaterialTgRefVersion)
        .join(MaterialAssetVersion, _asset_version_join())
        .join(MaterialTgRefVersion, _tg_ref_version_join())
        .where(
            Material.id.in_(material_ids),
            *_ready_filters(tenant_id, group.group_type),
        )
        .order_by(Material.id)
    )
    return [item for item in session.execute(statement) if static_image(item[0])]


def asset_manifest(
    item: tuple[Material, MaterialAssetVersion, MaterialTgRefVersion],
) -> dict:
    material, asset, tg_ref = item
    return {
        "material_id": material.id,
        "asset_version_id": asset.asset_version_id,
        "asset_fingerprint": asset.asset_fingerprint,
        "tg_ref_version_id": tg_ref.tg_ref_version_id,
        "tg_cache_peer_id": tg_ref.tg_cache_peer_id,
        "tg_cache_message_id": tg_ref.tg_cache_message_id,
    }


def selected_image_available(
    session: Session,
    row: CommentFallbackSelection,
) -> bool:
    material = session.get(Material, int(row.material_id or 0))
    return bool(
        material
        and material.tenant_id == row.tenant_id
        and material.asset_version_id == row.asset_version_id
        and material.asset_fingerprint == row.asset_fingerprint
        and material.tg_ref_version_id == row.tg_ref_version_id
        and material.review_status == "已审核"
        and material.cache_ready_status == "ready"
        and material.delivery_mode == "download_reupload"
        and material.emoji_asset_kind == "image_meme"
        and material.tg_cache_peer_id == row.tg_cache_peer_id
        and material.tg_cache_message_id == row.tg_cache_message_id
        and static_image(material)
    )


def static_image(material: Material) -> bool:
    mime = str(material.mime_type or "").lower()
    name = str(material.file_name or material.content or "").lower()
    if mime:
        return mime in STATIC_IMAGE_MIME_TYPES
    return name.endswith(STATIC_IMAGE_EXTENSIONS)


def _asset_version_join():
    return (
        (MaterialAssetVersion.material_id == Material.id)
        & (MaterialAssetVersion.asset_version_id == Material.asset_version_id)
    )


def _tg_ref_version_join():
    return (
        (MaterialTgRefVersion.material_id == Material.id)
        & (MaterialTgRefVersion.tg_ref_version_id == Material.tg_ref_version_id)
    )


def _ready_filters(tenant_id: int, group_type: str) -> tuple:
    return (
        Material.tenant_id == tenant_id,
        Material.material_type == group_type,
        Material.emoji_asset_kind == "image_meme",
        Material.review_status == "已审核",
        Material.cache_ready_status == "ready",
        Material.delivery_mode == "download_reupload",
        Material.tg_cache_peer_id != "",
        Material.tg_cache_message_id != "",
    )


__all__ = ["asset_manifest", "ready_image_assets", "selected_image_available"]

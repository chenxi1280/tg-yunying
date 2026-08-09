from __future__ import annotations

import json
import os
import sys
import traceback
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.database import SessionLocal
from app.models import AvatarMaterialSource, Material
from app.services._common import audit
from app.services.ai_config import create_uploaded_material
from app.services.avatar_material_import import AvatarSourceInput, inspect_avatar_source
from app.services.material_ingestion import save_material_upload_temp

USER_AGENT = "tg-yunying-avatar-curation/1.0 (https://github.com/chenxi1280/tg-yunying)"
MAX_DOWNLOAD_BYTES = 10 * 1024 * 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001 - urllib hook.
        return None


@dataclass(frozen=True)
class ImportContext:
    item: dict[str, Any]
    tenant_id: int
    manifest_sha256: str
    approval_ref: str


def main() -> int:
    context = _load_context()
    material_id = main_with_context(context)
    print(material_id, flush=True)
    return 0


def main_with_context(context: ImportContext) -> int:
    with SessionLocal() as session:
        existing_id, needs_repair = _existing_material_state(session, context)
    if existing_id is not None and not needs_repair:
        return existing_id
    data = _fetch_bytes(str(context.item["source"]["source_file_url"]))
    _assert_manifest_item(context.item, data)
    with SessionLocal() as session:
        if existing_id is not None:
            return _repair_material(session, context, data)
        return int(_create_material(session, context, data).id)


def _load_context() -> ImportContext:
    item = json.loads(_required_env("AVATAR_ITEM_JSON"))
    manifest_sha256 = _required_env("AVATAR_ITEM_MANIFEST_SHA256").lower()
    approval_ref = _required_env("AVATAR_ITEM_APPROVAL_REF")
    tenant_id = int(_required_env("AVATAR_ITEM_TENANT_ID"))
    if not isinstance(item, dict) or not isinstance(item.get("source"), dict):
        raise ValueError("AVATAR_ITEM_JSON must contain one source item")
    if len(manifest_sha256) != 64:
        raise ValueError("AVATAR_ITEM_MANIFEST_SHA256 must be 64 characters")
    return ImportContext(item, tenant_id, manifest_sha256, approval_ref)


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


def _existing_material_state(session, context: ImportContext) -> tuple[int | None, bool]:
    page_id = str(context.item["page_id"])
    stmt = (
        select(Material)
        .join(AvatarMaterialSource, AvatarMaterialSource.material_id == Material.id)
        .where(
            AvatarMaterialSource.tenant_id == context.tenant_id,
            AvatarMaterialSource.source_page_id == page_id,
        )
    )
    material = session.scalar(stmt)
    if material is None:
        return None, False
    has_file = bool(material.content and Path(material.content).is_file())
    return int(material.id), material.cache_ready_status != "ready" and not has_file


def _fetch_bytes(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    opener = urllib.request.build_opener(_NoRedirect)
    with opener.open(request, timeout=30) as response:
        data = response.read(MAX_DOWNLOAD_BYTES + 1)
    if len(data) > MAX_DOWNLOAD_BYTES:
        raise ValueError(f"avatar source exceeds {MAX_DOWNLOAD_BYTES} bytes")
    return data


def _assert_manifest_item(item: dict[str, Any], data: bytes) -> None:
    source = AvatarSourceInput(**item["source"])
    prepared = inspect_avatar_source(data=data, source=source)
    keys = ("content_sha256", "perceptual_hash", "width", "height", "detected_mime_type")
    expected = {key: item[key] for key in keys}
    actual = {key: getattr(prepared, key) for key in keys}
    if actual != expected:
        raise RuntimeError(f"avatar manifest item drift: page_id={item['page_id']}")


def _repair_material(session, context: ImportContext, data: bytes) -> int:
    source, material = _existing_source_and_material(session, context)
    _assert_existing_source(item=context.item, source=source, material=material, file_size=len(data))
    if material.cache_ready_status == "ready" or (material.content and Path(material.content).is_file()):
        return int(material.id)
    path, normalized_type, fingerprint = save_material_upload_temp(
        tenant_id=context.tenant_id,
        filename=str(context.item["filename"]),
        content_type=str(context.item["mime_type"]),
        data=data,
        material_type="图片",
    )
    _restore_material_fields(
        material,
        item=context.item,
        path=path,
        normalized_type=normalized_type,
        fingerprint=fingerprint,
        file_size=len(data),
    )
    actor = _actor(context)
    audit(
        session,
        tenant_id=context.tenant_id,
        actor=actor,
        action="恢复许可头像素材文件",
        target_type="material",
        target_id=str(material.id),
        detail=f"page_id={source.source_page_id};manifest_sha256={context.manifest_sha256}",
    )
    session.commit()
    return int(material.id)


def _existing_source_and_material(session, context: ImportContext) -> tuple[AvatarMaterialSource, Material]:
    stmt = (
        select(AvatarMaterialSource, Material)
        .join(Material, Material.id == AvatarMaterialSource.material_id)
        .where(
            AvatarMaterialSource.tenant_id == context.tenant_id,
            AvatarMaterialSource.source_page_id == str(context.item["page_id"]),
        )
    )
    row = session.execute(stmt).one_or_none()
    if row is None:
        raise RuntimeError(f"existing avatar source disappeared: page_id={context.item['page_id']}")
    return row


def _assert_existing_source(
    *,
    item: dict[str, Any],
    source: AvatarMaterialSource,
    material: Material,
    file_size: int,
) -> None:
    source_input = item["source"]
    expected = {
        "source_page_id": str(item["page_id"]),
        "source_page_url": str(source_input["source_page_url"]),
        "source_file_url": str(source_input["source_file_url"]),
        "license_code": str(source_input["license_code"]),
        "license_url": str(source_input["license_url"]),
        "attribution_text": str(source_input["attribution_text"]),
        "content_sha256": str(item["content_sha256"]),
        "perceptual_hash": str(item["perceptual_hash"]),
        "contains_person": bool(source_input.get("contains_person", False)),
    }
    actual = {key: getattr(source, key) for key in expected}
    material_expected = (
        "图片",
        str(item["detected_mime_type"]),
        file_size,
        int(item["width"]),
        int(item["height"]),
    )
    material_actual = (material.material_type, material.mime_type, material.file_size, material.width, material.height)
    if actual != expected or material_actual != material_expected:
        raise RuntimeError(f"existing avatar source drift: page_id={item['page_id']}")


def _restore_material_fields(
    material: Material,
    *,
    item: dict[str, Any],
    path: Path,
    normalized_type: str,
    fingerprint: str,
    file_size: int,
) -> None:
    material.content = str(path)
    material.source_kind = "upload"
    material.asset_fingerprint = fingerprint
    material.file_name = str(item["filename"])
    material.mime_type = normalized_type
    material.file_size = file_size
    material.width = int(item["width"])
    material.height = int(item["height"])
    material.cache_ready_status = "not_cached"
    material.last_cache_error = ""


def _actor(context: ImportContext) -> str:
    return f"github-actions-avatar-material-import:{context.approval_ref}:{context.manifest_sha256[:12]}"


def _create_material(session, context: ImportContext, data: bytes):
    item = context.item
    source = AvatarSourceInput(**item["source"])
    actor = _actor(context)
    return create_uploaded_material(
        session,
        tenant_id=context.tenant_id,
        title=str(item["title"]),
        material_type="图片",
        tags="头像,Commons,许可素材",
        caption=f"{source.attribution_text} · {source.license_code}",
        filename=str(item["filename"]),
        content_type=str(item["mime_type"]),
        data=data,
        actor=actor,
        avatar_source=source,
        attach_reference_summary=False,
    )


def _run_cli() -> None:
    exit_code = 0
    try:
        exit_code = main()
    except BaseException:  # noqa: BLE001 - one-shot child must return a nonzero process status.
        traceback.print_exc()
        exit_code = 1
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(exit_code)


if __name__ == "__main__":
    _run_cli()

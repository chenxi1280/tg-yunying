from __future__ import annotations

import json
import os
import sys
import traceback
import urllib.request
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from app.database import SessionLocal
from app.models import AvatarMaterialSource
from app.services.ai_config import create_uploaded_material
from app.services.avatar_material_import import AvatarSourceInput, inspect_avatar_source

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
    with SessionLocal() as session:
        existing_id = _existing_material_id(session, context)
    if existing_id is not None:
        print(existing_id, flush=True)
        return 0
    data = _fetch_bytes(str(context.item["source"]["source_file_url"]))
    _assert_manifest_item(context.item, data)
    with SessionLocal() as session:
        material = _create_material(session, context, data)
    print(int(material.id), flush=True)
    return 0


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


def _existing_material_id(session, context: ImportContext) -> int | None:
    page_id = str(context.item["page_id"])
    stmt = select(AvatarMaterialSource.material_id).where(
        AvatarMaterialSource.tenant_id == context.tenant_id,
        AvatarMaterialSource.source_page_id == page_id,
    )
    material_id = session.scalar(stmt)
    return int(material_id) if material_id is not None else None


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


def _create_material(session, context: ImportContext, data: bytes):
    item = context.item
    source = AvatarSourceInput(**item["source"])
    actor = f"github-actions-avatar-material-import:{context.approval_ref}:{context.manifest_sha256[:12]}"
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

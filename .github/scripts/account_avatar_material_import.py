from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import asdict
from typing import Any

from sqlalchemy import select

from app.database import SessionLocal
from app.models import AvatarMaterialSource, Material
from app.services.avatar_material_import import (
    AvatarSourceInput,
    assert_avatar_candidates_importable,
    inspect_avatar_source,
)

COMMONS_API_URL = "https://commons.wikimedia.org/w/api.php"
CURATED_PAGE_IDS = (
    "1260937", "2336845", "4263305", "21588943", "24217195", "27721295",
    "30261936", "30261948", "30261973", "30261994", "30262007", "36201256",
    "92761445", "99754952", "129259980", "129259982", "129259983",
)
MODE = os.getenv("AVATAR_MATERIAL_IMPORT_MODE", "preview").strip().lower()
TENANT_ID = int(os.getenv("AVATAR_MATERIAL_IMPORT_TENANT_ID", "1"))
EXPECTED_SHA256 = os.getenv("AVATAR_MATERIAL_IMPORT_EXPECTED_SHA256", "").strip().lower()
DEPLOYED_SHA = os.getenv("AVATAR_MATERIAL_IMPORT_DEPLOYED_SHA", "").strip()
APPROVAL_REF = os.getenv("AVATAR_MATERIAL_IMPORT_APPROVAL_REF", "").strip()
VALID_MODES = {"preview", "apply", "readback"}
USER_AGENT = "tg-yunying-avatar-curation/1.0 (https://github.com/chenxi1280/tg-yunying)"
MAX_DOWNLOAD_BYTES = 10 * 1024 * 1024
DOWNLOAD_INTERVAL_SECONDS = 0.75


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001 - urllib hook.
        return None


def main() -> int:
    _validate_inputs()
    if MODE == "readback":
        output = avatar_material_readback()
        print("ACCOUNT_AVATAR_MATERIAL_READBACK=" + json.dumps(output, ensure_ascii=False, sort_keys=True), flush=True)
        if not output["complete"]:
            raise RuntimeError("account avatar material readback is incomplete")
        return 0
    with SessionLocal() as session:
        manifest = build_manifest(session, deployed_sha=DEPLOYED_SHA)
    manifest_sha = manifest_sha256(manifest)
    imported_ids = _apply(manifest, manifest_sha) if MODE == "apply" else []
    with SessionLocal() as session:
        imported_page_ids = _imported_page_ids(session, TENANT_ID)
    output = {
        "mode": MODE,
        "manifest": manifest,
        "manifest_sha256": manifest_sha,
        "manifest_sha256_b64": _sha256_b64(manifest_sha),
        "imported_material_ids": imported_ids,
        "curated_page_count": len(CURATED_PAGE_IDS),
        "curated_pages_imported_count": len(imported_page_ids.intersection(CURATED_PAGE_IDS)),
    }
    print("ACCOUNT_AVATAR_MATERIAL_IMPORT=" + json.dumps(output, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


def _validate_inputs() -> None:
    if MODE not in VALID_MODES:
        raise ValueError(f"unsupported mode: {MODE}")
    if not DEPLOYED_SHA:
        raise ValueError("AVATAR_MATERIAL_IMPORT_DEPLOYED_SHA is required")
    if MODE not in {"apply", "readback"}:
        return
    if not EXPECTED_SHA256:
        raise ValueError("AVATAR_MATERIAL_IMPORT_EXPECTED_SHA256 is required for apply/readback")
    if MODE == "apply" and not APPROVAL_REF:
        raise ValueError("AVATAR_MATERIAL_IMPORT_APPROVAL_REF is required for apply")


def build_manifest(session, *, deployed_sha: str) -> dict[str, Any]:
    pages = _fetch_commons_pages()
    items: list[dict[str, Any]] = []
    prepared_candidates = []
    for index, page_id in enumerate(CURATED_PAGE_IDS):
        source, metadata = _source_from_page(page_id, pages[page_id])
        data = _fetch_bytes(source.source_file_url)
        prepared = inspect_avatar_source(data=data, source=source)
        items.append(
            {
                "page_id": page_id,
                "title": metadata["title"],
                "mime_type": prepared.detected_mime_type,
                "filename": metadata["filename"],
                **asdict(prepared),
                "source": asdict(source),
            }
        )
        prepared_candidates.append(prepared)
        if index + 1 < len(CURATED_PAGE_IDS):
            time.sleep(DOWNLOAD_INTERVAL_SECONDS)
    assert_avatar_candidates_importable(session, tenant_id=TENANT_ID, candidates=prepared_candidates)
    return {"tenant_id": TENANT_ID, "deployed_sha": deployed_sha, "items": items}


def avatar_material_readback() -> dict[str, Any]:
    with SessionLocal() as session:
        rows = _avatar_readback_rows(session, TENANT_ID)
    results = [_avatar_readback_item(source, material) for source, material in rows]
    found_ids = {result["page_id"] for result in results}
    missing_ids = sorted(set(CURATED_PAGE_IDS) - found_ids)
    ready_count = sum(result["status"] == "ready" for result in results)
    return {
        "mode": MODE,
        "manifest_sha256": EXPECTED_SHA256,
        "curated_page_count": len(CURATED_PAGE_IDS),
        "found_count": len(found_ids),
        "ready_count": ready_count,
        "missing_page_ids": missing_ids,
        "results": results,
        "complete": not missing_ids and ready_count == len(CURATED_PAGE_IDS),
    }


def _avatar_readback_rows(session, tenant_id: int) -> list[tuple[AvatarMaterialSource, Material]]:
    stmt = (
        select(AvatarMaterialSource, Material)
        .join(Material, Material.id == AvatarMaterialSource.material_id)
        .where(
            AvatarMaterialSource.tenant_id == tenant_id,
            AvatarMaterialSource.source_page_id.in_(CURATED_PAGE_IDS),
        )
        .order_by(AvatarMaterialSource.source_page_id.asc())
    )
    return list(session.execute(stmt))


def _avatar_readback_item(source: AvatarMaterialSource, material: Material) -> dict[str, Any]:
    cache_ready = (
        material.review_status == "已审核"
        and material.cache_ready_status == "ready"
        and bool(material.tg_cache_peer_id)
        and bool(material.tg_cache_message_id)
        and material.tg_cache_account_id is not None
    )
    return {
        "page_id": source.source_page_id,
        "material_id": int(material.id),
        "status": "ready" if cache_ready else "not_ready",
        "review_status": material.review_status,
        "cache_ready_status": material.cache_ready_status,
    }


def _fetch_commons_pages() -> dict[str, dict[str, Any]]:
    query = urllib.parse.urlencode(
        {
            "action": "query",
            "format": "json",
            "pageids": "|".join(CURATED_PAGE_IDS),
            "prop": "imageinfo",
            "iiprop": "url|mime|extmetadata",
            "iiurlwidth": "1024",
        }
    )
    payload = json.loads(_fetch_bytes(f"{COMMONS_API_URL}?{query}").decode("utf-8"))
    pages = payload.get("query", {}).get("pages", {})
    missing = [
        page_id
        for page_id in CURATED_PAGE_IDS
        if page_id not in pages or "missing" in pages[page_id] or not pages[page_id].get("imageinfo")
    ]
    if missing:
        raise RuntimeError(f"Wikimedia Commons pages missing: {missing}")
    return pages


def _source_from_page(page_id: str, page: dict[str, Any]) -> tuple[AvatarSourceInput, dict[str, str]]:
    image_info = page["imageinfo"][0]
    metadata = image_info.get("extmetadata", {})
    title = str(page["title"])
    source = AvatarSourceInput(
        source_page_id=page_id,
        source_page_url=str(image_info.get("descriptionurl") or ""),
        source_file_url=str(image_info.get("thumburl") or image_info["url"]),
        license_code=_metadata_value(metadata, "LicenseShortName"),
        license_url=_metadata_value(metadata, "LicenseUrl"),
        attribution_text=_attribution(metadata, title),
    )
    return source, {
        "title": title.removeprefix("File:")[:160],
        "filename": _filename(source.source_file_url),
    }


def _metadata_value(metadata: dict[str, Any], key: str) -> str:
    return str(metadata.get(key, {}).get("value") or "").strip()


def _attribution(metadata: dict[str, Any], fallback: str) -> str:
    artist = _plain_text(_metadata_value(metadata, "Artist"))
    credit = _plain_text(_metadata_value(metadata, "Credit"))
    return artist or credit or fallback


def _plain_text(value: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", value)).split())[:1000]


def _filename(url: str) -> str:
    return urllib.parse.unquote(urllib.parse.urlparse(url).path.rsplit("/", 1)[-1])[:255]


def _fetch_bytes(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    opener = urllib.request.build_opener(_NoRedirect)
    with opener.open(request, timeout=30) as response:
        data = response.read(MAX_DOWNLOAD_BYTES + 1)
    if len(data) > MAX_DOWNLOAD_BYTES:
        raise ValueError(f"avatar source exceeds {MAX_DOWNLOAD_BYTES} bytes")
    return data


def _imported_page_ids(session, tenant_id: int) -> set[str]:
    return set(session.scalars(select(AvatarMaterialSource.source_page_id).where(AvatarMaterialSource.tenant_id == tenant_id)))


def manifest_sha256(manifest: dict[str, Any]) -> str:
    encoded = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sha256_b64(value: str) -> str:
    return base64.b64encode(bytes.fromhex(value)).decode("ascii")


def _apply(manifest: dict[str, Any], manifest_sha: str) -> list[int]:
    if manifest_sha != EXPECTED_SHA256:
        raise RuntimeError(f"manifest hash mismatch: expected={EXPECTED_SHA256};actual={manifest_sha}")
    imported_ids: list[int] = []
    for item in manifest["items"]:
        with SessionLocal() as session:
            if item["page_id"] in _imported_page_ids(session, TENANT_ID):
                continue
        imported_ids.append(_apply_item_isolated(item, manifest_sha))
    return imported_ids


def _apply_item_isolated(item: dict[str, Any], manifest_sha: str) -> int:
    environment = dict(os.environ)
    environment.update(
        {
            "AVATAR_ITEM_JSON": json.dumps(item, ensure_ascii=False, sort_keys=True),
            "AVATAR_ITEM_MANIFEST_SHA256": manifest_sha,
            "AVATAR_ITEM_APPROVAL_REF": APPROVAL_REF,
            "AVATAR_ITEM_TENANT_ID": str(TENANT_ID),
        }
    )
    result = subprocess.run(
        [sys.executable, "-m", "app.services.avatar_material_import_worker"],
        env=environment,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()[-1:]
        raise RuntimeError(
            f"avatar item process failed: page_id={item['page_id']};"
            f"exit_code={result.returncode};detail={''.join(detail)[:240]}"
        )
    output = result.stdout.strip().splitlines()
    if not output:
        raise RuntimeError("avatar item process returned no material id")
    return int(output[-1])


if __name__ == "__main__":
    raise SystemExit(main())

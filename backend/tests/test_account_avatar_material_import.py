from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import AvatarMaterialSource, Material, Tenant

pytestmark = pytest.mark.no_postgres


def _load_script():
    path = Path(__file__).resolve().parents[2] / ".github/scripts/account_avatar_material_import.py"
    spec = spec_from_file_location("account_avatar_material_import", path)
    assert spec and spec.loader
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _image_bytes() -> bytes:
    image = Image.new("RGB", (300, 300), "navy")
    buffer = BytesIO()
    image.save(buffer, format="JPEG")
    return buffer.getvalue()


def _page(page_id: str) -> dict:
    return {
        "title": f"File:Avatar-{page_id}.jpg",
        "imageinfo": [
            {
                "descriptionurl": f"https://commons.wikimedia.org/wiki/File:Avatar-{page_id}.jpg",
                "thumburl": f"https://upload.wikimedia.org/avatar-{page_id}.jpg",
                "thumbmime": "image/jpeg",
                "url": f"https://upload.wikimedia.org/original-{page_id}.jpg",
                "extmetadata": {
                    "LicenseShortName": {"value": "CC BY 4.0"},
                    "LicenseUrl": {"value": "https://creativecommons.org/licenses/by/4.0/"},
                    "Artist": {"value": "<a>Example Author</a>"},
                },
            }
        ],
    }


def test_manifest_is_stable_and_contains_license(monkeypatch):
    script = _load_script()
    monkeypatch.setattr(script, "CURATED_PAGE_IDS", ("101",))
    monkeypatch.setattr(script, "_fetch_commons_pages", lambda: {"101": _page("101")})
    monkeypatch.setattr(script, "_fetch_bytes", lambda _url: _image_bytes())
    monkeypatch.setattr(script.time, "sleep", lambda _seconds: None)
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.commit()
        first = script.build_manifest(session, deployed_sha="abc123")
        second = script.build_manifest(session, deployed_sha="abc123")

    assert first == second
    assert first["items"][0]["source"]["license_code"] == "CC BY 4.0"
    assert first["items"][0]["source"]["attribution_text"] == "Example Author"
    assert "already_imported" not in first["items"][0]
    assert len(first["items"][0]["content_sha256"]) == 64
    assert script.manifest_sha256(first) == script.manifest_sha256(second)


def test_apply_rejects_manifest_hash_mismatch(monkeypatch):
    script = _load_script()
    monkeypatch.setattr(script, "EXPECTED_SHA256", "0" * 64)
    with pytest.raises(RuntimeError, match="manifest hash mismatch"):
        script._apply({"items": []}, "1" * 64)


def test_apply_redownload_rejects_item_drift(monkeypatch):
    script = _load_script()
    source = script.AvatarSourceInput(
        source_page_id="101",
        source_page_url="https://commons.wikimedia.org/wiki/File:Avatar-101.jpg",
        source_file_url="https://upload.wikimedia.org/avatar-101.jpg",
        license_code="CC BY 4.0",
        license_url="https://creativecommons.org/licenses/by/4.0/",
        attribution_text="Example Author",
    )
    prepared = script.inspect_avatar_source(data=_image_bytes(), source=source)
    item = {
        "page_id": "101",
        "source": prepared.source.__dict__,
        "content_sha256": "0" * 64,
        "perceptual_hash": prepared.perceptual_hash,
        "width": prepared.width,
        "height": prepared.height,
        "detected_mime_type": prepared.detected_mime_type,
    }
    monkeypatch.setattr(script, "_fetch_bytes", lambda _url: _image_bytes())

    with pytest.raises(RuntimeError, match="avatar manifest item drift"):
        script._download_manifest_item(item)


def test_readback_requires_reviewed_tg_cache_ready_material():
    script = _load_script()
    script.CURATED_PAGE_IDS = ("101",)
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        material = Material(
            tenant_id=1,
            title="头像素材",
            material_type="图片",
            content="/tmp/avatar-101.jpg",
            review_status="已审核",
            source_kind="upload",
            cache_ready_status="ready",
            tg_cache_peer_id="@avatar_cache",
            tg_cache_message_id="100",
            tg_cache_account_id=1,
        )
        session.add(material)
        session.flush()
        source = AvatarMaterialSource(
            tenant_id=1,
            material_id=material.id,
            source_page_id="101",
            source_page_url="https://commons.wikimedia.org/wiki/File:Avatar-101.jpg",
            source_file_url="https://upload.wikimedia.org/avatar-101.jpg",
            license_code="CC BY 4.0",
            license_url="https://creativecommons.org/licenses/by/4.0/",
            attribution_text="Example Author",
            content_sha256="a" * 64,
            perceptual_hash="0" * 16,
        )
        session.add(source)
        session.commit()

        rows = script._avatar_readback_rows(session, 1)
        result = script._avatar_readback_item(*rows[0])

    assert result["status"] == "ready"

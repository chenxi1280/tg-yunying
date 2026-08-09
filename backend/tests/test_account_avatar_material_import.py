from __future__ import annotations

import subprocess
from importlib.util import module_from_spec, spec_from_file_location
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import AvatarMaterialSource, Material, Tenant
from app.services import avatar_material_import_worker as worker

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


def test_apply_uses_one_isolated_process_per_missing_item(monkeypatch):
    script = _load_script()
    script.EXPECTED_SHA256 = "a" * 64
    calls: list[str] = []
    monkeypatch.setattr(script, "SessionLocal", lambda: _empty_session())
    monkeypatch.setattr(
        script,
        "_apply_item_isolated",
        lambda item, _sha: calls.append(item["page_id"]) or int(item["page_id"]),
    )

    imported = script._apply({"items": [{"page_id": "101"}, {"page_id": "102"}]}, "a" * 64)

    assert calls == ["101", "102"]
    assert imported == [101, 102]


def test_isolated_item_process_uses_fresh_python(monkeypatch):
    script = _load_script()
    script.APPROVAL_REF = "approval-1"
    script.TENANT_ID = 7
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="321\n", stderr="")

    monkeypatch.setattr(script.subprocess, "run", run)

    material_id = script._apply_item_isolated({"page_id": "101"}, "a" * 64)

    assert material_id == 321
    assert calls[0][0] == [script.sys.executable, "-m", "app.services.avatar_material_import_worker"]
    assert calls[0][1]["env"]["AVATAR_ITEM_TENANT_ID"] == "7"
    assert calls[0][1]["env"]["AVATAR_ITEM_APPROVAL_REF"] == "approval-1"


def test_item_worker_accepts_exact_manifest_image():
    source = _worker_source()
    prepared = worker.inspect_avatar_source(data=_image_bytes(), source=source)
    item = {
        "page_id": "101",
        "source": source.__dict__,
        "content_sha256": prepared.content_sha256,
        "perceptual_hash": prepared.perceptual_hash,
        "width": prepared.width,
        "height": prepared.height,
        "detected_mime_type": prepared.detected_mime_type,
    }

    worker._assert_manifest_item(item, _image_bytes())


def test_item_worker_rejects_manifest_drift():
    source = _worker_source()
    prepared = worker.inspect_avatar_source(data=_image_bytes(), source=source)
    item = {
        "page_id": "101",
        "source": source.__dict__,
        "content_sha256": "0" * 64,
        "perceptual_hash": prepared.perceptual_hash,
        "width": prepared.width,
        "height": prepared.height,
        "detected_mime_type": prepared.detected_mime_type,
    }

    with pytest.raises(RuntimeError, match="avatar manifest item drift"):
        worker._assert_manifest_item(item, _image_bytes())


def test_item_worker_skips_expensive_material_reference_summary(monkeypatch):
    calls = []
    context = worker.ImportContext(
        item={
            "page_id": "101",
            "title": "Avatar 101",
            "filename": "avatar-101.jpg",
            "mime_type": "image/jpeg",
            "source": _worker_source().__dict__,
        },
        tenant_id=1,
        manifest_sha256="a" * 64,
        approval_ref="approval-1",
    )
    monkeypatch.setattr(worker, "create_uploaded_material", lambda *args, **kwargs: calls.append(kwargs) or object())

    worker._create_material(object(), context, _image_bytes())

    assert calls[0]["attach_reference_summary"] is False


def test_item_worker_hard_exits_after_success(monkeypatch):
    exits = []
    monkeypatch.setattr(worker, "main", lambda: 0)
    monkeypatch.setattr(worker.os, "_exit", lambda code: exits.append(code))

    worker._run_cli()

    assert exits == [0]


def _worker_source():
    return worker.AvatarSourceInput(
        source_page_id="101",
        source_page_url="https://commons.wikimedia.org/wiki/File:Avatar-101.jpg",
        source_file_url="https://upload.wikimedia.org/avatar-101.jpg",
        license_code="CC BY 4.0",
        license_url="https://creativecommons.org/licenses/by/4.0/",
        attribution_text="Example Author",
    )


def _empty_session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return Session(engine)


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

from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Tenant

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
        first, _ = script.build_manifest(session, deployed_sha="abc123")
        second, _ = script.build_manifest(session, deployed_sha="abc123")

    assert first == second
    assert first["items"][0]["source"]["license_code"] == "CC BY 4.0"
    assert first["items"][0]["source"]["attribution_text"] == "Example Author"
    assert len(first["items"][0]["content_sha256"]) == 64
    assert script.manifest_sha256(first) == script.manifest_sha256(second)


def test_apply_rejects_manifest_hash_mismatch(monkeypatch):
    script = _load_script()
    monkeypatch.setattr(script, "EXPECTED_SHA256", "0" * 64)
    with pytest.raises(RuntimeError, match="manifest hash mismatch"):
        script._apply({"items": []}, {}, "1" * 64)

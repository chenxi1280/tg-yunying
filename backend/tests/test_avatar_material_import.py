from __future__ import annotations

from io import BytesIO

import pytest
from PIL import Image
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import Base
from app.models import AvatarMaterialSource, Tenant
from app.services.ai_config import create_uploaded_material
from app.services.avatar_material_import import (
    AvatarSourceInput,
    assert_avatar_candidates_importable,
    inspect_avatar_source,
    prepare_avatar_source,
)

pytestmark = pytest.mark.no_postgres


def _image_bytes(*, inverted: bool = False) -> bytes:
    image = Image.new("RGB", (320, 320), "white" if not inverted else "black")
    for x in range(160):
        for y in range(320):
            image.putpixel((x, y), (20, 40, 80) if not inverted else (230, 200, 160))
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _source(page_id: str = "123") -> AvatarSourceInput:
    return AvatarSourceInput(
        source_page_id=page_id,
        source_page_url=f"https://commons.wikimedia.org/wiki/File:{page_id}.jpg",
        source_file_url=f"https://upload.wikimedia.org/{page_id}.jpg",
        license_code="CC BY 4.0",
        license_url="https://creativecommons.org/licenses/by/4.0/",
        attribution_text="Example photographer",
    )


def _session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session = Session(engine)
    session.add(Tenant(id=1, name="默认运营空间"))
    session.commit()
    return session


def test_uploaded_avatar_persists_license_and_hashes(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_ROOT", str(tmp_path))
    get_settings.cache_clear()
    data = _image_bytes()
    with _session() as session:
        material = create_uploaded_material(
            session,
            tenant_id=1,
            title="许可头像",
            material_type="图片",
            tags="头像",
            caption="Example photographer · CC BY 4.0",
            filename="avatar.png",
            content_type="image/png",
            data=data,
            avatar_source=_source(),
            actor="tester",
        )
        provenance = session.scalar(select(AvatarMaterialSource).where(AvatarMaterialSource.material_id == material.id))

    assert material.width == 320
    assert material.height == 320
    assert provenance is not None
    assert provenance.license_code == "CC BY 4.0"
    assert len(provenance.content_sha256) == 64
    assert len(provenance.perceptual_hash) == 16


def test_exact_duplicate_is_rejected():
    data = _image_bytes()
    with _session() as session:
        prepared = prepare_avatar_source(session, tenant_id=1, data=data, source=_source())
        session.add(
            AvatarMaterialSource(
                tenant_id=1,
                material_id=99,
                source_page_id="old",
                source_page_url="https://commons.wikimedia.org/wiki/File:old.jpg",
                source_file_url="https://upload.wikimedia.org/old.jpg",
                license_code="CC BY 4.0",
                license_url="https://creativecommons.org/licenses/by/4.0/",
                attribution_text="Old author",
                content_sha256=prepared.content_sha256,
                perceptual_hash=prepared.perceptual_hash,
            )
        )
        with pytest.raises(ValueError, match="完全重复"):
            prepare_avatar_source(session, tenant_id=1, data=data, source=_source("new"))


def test_perceptual_duplicate_is_rejected():
    data = _image_bytes()
    with _session() as session:
        prepared = prepare_avatar_source(session, tenant_id=1, data=data, source=_source())
        session.add(
            AvatarMaterialSource(
                tenant_id=1,
                material_id=98,
                source_page_id="old-near",
                source_page_url="https://commons.wikimedia.org/wiki/File:old-near.jpg",
                source_file_url="https://upload.wikimedia.org/old-near.jpg",
                license_code="CC BY 4.0",
                license_url="https://creativecommons.org/licenses/by/4.0/",
                attribution_text="Old author",
                content_sha256=prepared.content_sha256,
                perceptual_hash=prepared.perceptual_hash,
            )
        )
        with pytest.raises(ValueError, match="近似重复"):
            prepare_avatar_source(session, tenant_id=1, data=data + b"metadata", source=_source("new-near"))


def test_manifest_candidates_are_checked_against_each_other():
    data = _image_bytes()
    first = inspect_avatar_source(data=data, source=_source("first"))
    second = inspect_avatar_source(data=data, source=_source("second"))
    with _session() as session, pytest.raises(ValueError, match="完全重复"):
        assert_avatar_candidates_importable(session, tenant_id=1, candidates=[first, second])


@pytest.mark.parametrize(
    ("source", "message"),
    [
        (AvatarSourceInput("1", "http://commons.wikimedia.org/a", "https://upload.wikimedia.org/a.jpg", "CC BY 4.0", "https://creativecommons.org/licenses/by/4.0/", "author"), "仅允许 https"),
        (AvatarSourceInput("1", "https://commons.wikimedia.org/a", "https://upload.wikimedia.org/a.jpg", "All rights reserved", "https://creativecommons.org/licenses/by/4.0/", "author"), "许可不在允许清单"),
    ],
)
def test_invalid_source_is_rejected(source, message):
    with _session() as session, pytest.raises(ValueError, match=message):
        prepare_avatar_source(session, tenant_id=1, data=_image_bytes(), source=source)

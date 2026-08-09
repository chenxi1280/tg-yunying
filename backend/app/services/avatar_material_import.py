from __future__ import annotations

import hashlib
from dataclasses import dataclass
from io import BytesIO
from urllib.parse import urlparse

from PIL import Image, UnidentifiedImageError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AvatarMaterialSource

ALLOWED_COMMONS_HOSTS = frozenset({"commons.wikimedia.org", "upload.wikimedia.org"})
ALLOWED_LICENSE_CODES = frozenset({"CC0", "PDM", "CC BY 2.0", "CC BY 2.5", "CC BY 3.0", "CC BY 4.0", "CC BY-SA 3.0", "CC BY-SA 4.0"})
AVATAR_MIN_EDGE = 256
PERCEPTUAL_HASH_SIZE = 8
NEAR_DUPLICATE_DISTANCE = 5
IMAGE_FORMAT_MIMES = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp", "GIF": "image/gif"}


@dataclass(frozen=True)
class AvatarSourceInput:
    source_page_id: str
    source_page_url: str
    source_file_url: str
    license_code: str
    license_url: str
    attribution_text: str
    contains_person: bool = False


@dataclass(frozen=True)
class PreparedAvatarSource:
    source: AvatarSourceInput
    content_sha256: str
    perceptual_hash: str
    width: int
    height: int
    detected_mime_type: str


def prepare_avatar_source(
    session: Session,
    *,
    tenant_id: int,
    data: bytes,
    source: AvatarSourceInput,
) -> PreparedAvatarSource:
    prepared = inspect_avatar_source(data=data, source=source)
    existing = list(
        session.scalars(select(AvatarMaterialSource).where(AvatarMaterialSource.tenant_id == tenant_id))
    )
    _assert_not_duplicate(existing, prepared.content_sha256, prepared.perceptual_hash)
    return prepared


def inspect_avatar_source(*, data: bytes, source: AvatarSourceInput) -> PreparedAvatarSource:
    _validate_source(source)
    width, height, perceptual_hash, detected_mime_type = _inspect_image(data)
    content_sha256 = hashlib.sha256(data).hexdigest()
    return PreparedAvatarSource(
        source=source,
        content_sha256=content_sha256,
        perceptual_hash=perceptual_hash,
        width=width,
        height=height,
        detected_mime_type=detected_mime_type,
    )


def assert_avatar_candidates_importable(
    session: Session,
    *,
    tenant_id: int,
    candidates: list[PreparedAvatarSource],
) -> None:
    existing = list(
        session.scalars(select(AvatarMaterialSource).where(AvatarMaterialSource.tenant_id == tenant_id))
    )
    imported_pages = {item.source_page_id for item in existing}
    accepted: list[tuple[str, str, str]] = [
        (str(item.material_id), item.content_sha256, item.perceptual_hash) for item in existing
    ]
    for candidate in candidates:
        if candidate.source.source_page_id in imported_pages:
            continue
        _assert_fingerprints_unique(accepted, candidate.content_sha256, candidate.perceptual_hash)
        accepted.append((candidate.source.source_page_id, candidate.content_sha256, candidate.perceptual_hash))


def new_avatar_material_source(
    prepared: PreparedAvatarSource,
    *,
    tenant_id: int,
    material_id: int,
    actor: str,
) -> AvatarMaterialSource:
    source = prepared.source
    return AvatarMaterialSource(
        tenant_id=tenant_id,
        material_id=material_id,
        source_page_id=source.source_page_id.strip(),
        source_page_url=source.source_page_url.strip(),
        source_file_url=source.source_file_url.strip(),
        license_code=source.license_code.strip(),
        license_url=source.license_url.strip(),
        attribution_text=source.attribution_text.strip(),
        content_sha256=prepared.content_sha256,
        perceptual_hash=prepared.perceptual_hash,
        contains_person=source.contains_person,
        imported_by=actor.strip(),
    )


def _validate_source(source: AvatarSourceInput) -> None:
    if source.contains_person:
        raise ValueError("头像素材导入暂不允许包含可识别人物")
    if source.license_code.strip() not in ALLOWED_LICENSE_CODES:
        raise ValueError("头像素材许可不在允许清单")
    if not source.source_page_id.strip():
        raise ValueError("头像素材缺少来源页面 ID")
    if not source.attribution_text.strip():
        raise ValueError("头像素材缺少作者署名")
    _validate_url(source.source_page_url, expected_host="commons.wikimedia.org")
    _validate_url(source.source_file_url, expected_host="upload.wikimedia.org")
    _validate_url(source.license_url)


def _validate_url(value: str, *, expected_host: str | None = None) -> None:
    parsed = urlparse(value.strip())
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or not host:
        raise ValueError("头像素材来源仅允许 https")
    if expected_host and host != expected_host:
        raise ValueError(f"头像素材来源域名必须为 {expected_host}")
    trusted_license_host = host == "creativecommons.org" or host.endswith(".creativecommons.org")
    if not expected_host and host not in ALLOWED_COMMONS_HOSTS and not trusted_license_host:
        raise ValueError("头像素材许可地址域名不受信任")


def _inspect_image(data: bytes) -> tuple[int, int, str, str]:
    try:
        with Image.open(BytesIO(data)) as image:
            image.verify()
        with Image.open(BytesIO(data)) as image:
            width, height = image.size
            detected_mime_type = IMAGE_FORMAT_MIMES.get(str(image.format or "").upper())
            if not detected_mime_type:
                raise ValueError("头像素材图片格式不受支持")
            if min(width, height) < AVATAR_MIN_EDGE:
                raise ValueError(f"头像素材短边不得小于 {AVATAR_MIN_EDGE} 像素")
            grayscale = image.convert("L").resize((PERCEPTUAL_HASH_SIZE, PERCEPTUAL_HASH_SIZE))
            pixels = list(grayscale.get_flattened_data())
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError("头像素材不是有效图片") from exc
    average = sum(pixels) / len(pixels)
    bits = "".join("1" if pixel >= average else "0" for pixel in pixels)
    return width, height, f"{int(bits, 2):016x}", detected_mime_type


def _assert_not_duplicate(
    existing: list[AvatarMaterialSource],
    content_sha256: str,
    perceptual_hash: str,
) -> None:
    fingerprints = [(str(item.material_id), item.content_sha256, item.perceptual_hash) for item in existing]
    _assert_fingerprints_unique(fingerprints, content_sha256, perceptual_hash)


def _assert_fingerprints_unique(
    existing: list[tuple[str, str, str]],
    content_sha256: str,
    perceptual_hash: str,
) -> None:
    for identity, existing_sha, existing_perceptual_hash in existing:
        if existing_sha == content_sha256:
            raise ValueError(f"头像素材与 {identity} 完全重复")
        if _hash_distance(existing_perceptual_hash, perceptual_hash) <= NEAR_DUPLICATE_DISTANCE:
            raise ValueError(f"头像素材与 {identity} 近似重复")


def _hash_distance(left: str, right: str) -> int:
    return (int(left, 16) ^ int(right, 16)).bit_count()

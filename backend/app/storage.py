from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from .config import get_settings


CONTENT_TYPE_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


def media_root() -> Path:
    root = Path(get_settings().media_root)
    root.mkdir(parents=True, exist_ok=True)
    return root


def save_avatar_bytes(*, tenant_id: int, account_id: int, content_type: str, data: bytes) -> tuple[str, Path]:
    extension = CONTENT_TYPE_EXTENSIONS.get(content_type)
    if not extension:
        raise ValueError("unsupported avatar content type")
    object_key = f"avatars/{tenant_id}/{account_id}/{uuid4().hex}{extension}"
    path = media_root() / object_key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return object_key, path


def object_path(object_key: str) -> Path:
    return media_root() / object_key


def preview_url(object_key: str) -> str:
    return f"/media/{object_key}" if object_key else ""

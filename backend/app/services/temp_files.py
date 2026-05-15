from __future__ import annotations

import shutil
from pathlib import Path
from time import time

from app.storage import media_root

TEMP_SUBDIRS = ("tmp", "material-tmp", "source-media-tmp", "previews")
TEMP_FILE_TTL_SECONDS = 10 * 24 * 60 * 60


def temp_dir(name: str = "tmp") -> Path:
    if name not in TEMP_SUBDIRS:
        raise ValueError("unknown temp dir")
    path = media_root() / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def cleanup_temp_files(*, ttl_seconds: int = TEMP_FILE_TTL_SECONDS, now_ts: float | None = None) -> int:
    root = media_root().resolve()
    cutoff = (now_ts if now_ts is not None else time()) - ttl_seconds
    removed = 0
    for subdir in TEMP_SUBDIRS:
        path = (root / subdir).resolve()
        if not str(path).startswith(str(root)) or not path.exists():
            continue
        for item in sorted(path.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            if not item.exists():
                continue
            if item.is_dir():
                if not any(item.iterdir()):
                    item.rmdir()
                continue
            if item.stat().st_mtime <= cutoff:
                item.unlink()
                removed += 1
        if path.exists() and not any(path.iterdir()):
            path.rmdir()
    return removed


def clear_temp_dir(name: str) -> None:
    path = temp_dir(name)
    if path.exists():
        shutil.rmtree(path)


__all__ = ["TEMP_FILE_TTL_SECONDS", "TEMP_SUBDIRS", "cleanup_temp_files", "clear_temp_dir", "temp_dir"]

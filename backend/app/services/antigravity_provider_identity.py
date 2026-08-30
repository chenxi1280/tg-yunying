from __future__ import annotations

from urllib.parse import urlparse


def canonical_antigravity_base_url(value: str) -> str:
    parsed = urlparse(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("antigravity_base_url_invalid") from exc
    if not parsed.scheme or not parsed.hostname or port is None:
        raise ValueError("antigravity_base_url_invalid")
    path = parsed.path.rstrip("/")
    return f"{parsed.scheme.lower()}://{parsed.hostname.lower()}:{port}{path}"


__all__ = ["canonical_antigravity_base_url"]

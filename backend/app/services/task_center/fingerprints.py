from __future__ import annotations

import hashlib
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import MessageFingerprint
from app.services._common import _now


def content_fingerprint(content: str) -> str:
    normalized = " ".join((content or "").strip().lower().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def fingerprint_exists(
    session: Session,
    tenant_id: int,
    source_group_id: int | str,
    content: str,
    *,
    window_minutes: int | None = None,
) -> bool:
    stmt = select(MessageFingerprint.id).where(
        MessageFingerprint.tenant_id == tenant_id,
        MessageFingerprint.source_group_id == str(source_group_id),
        MessageFingerprint.fingerprint == content_fingerprint(content),
    )
    if window_minutes is not None:
        stmt = stmt.where(MessageFingerprint.created_at >= _now() - timedelta(minutes=window_minutes))
    return bool(session.scalar(stmt))


def is_duplicate(session: Session, tenant_id: int, source_group_id: int | str, content: str, *, window_minutes: int) -> bool:
    return fingerprint_exists(session, tenant_id, source_group_id, content, window_minutes=window_minutes)


def remember_fingerprint(session: Session, tenant_id: int, source_group_id: int | str, content: str) -> None:
    session.add(
        MessageFingerprint(
            tenant_id=tenant_id,
            source_group_id=str(source_group_id),
            fingerprint=content_fingerprint(content),
            semantic_hash="",
            original_text=content[:4000],
        )
    )


__all__ = ["content_fingerprint", "fingerprint_exists", "is_duplicate", "remember_fingerprint"]

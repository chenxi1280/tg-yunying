"""Explicit tenant rollout state for the AI group continuity ledger."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import SchedulingSetting

from .datetime_compat import ensure_aware


def continuity_enabled(session: Session, tenant_id: int) -> bool:
    setting = _setting(session, tenant_id)
    return bool(setting and setting.ai_group_send_continuity_v1)


def continuity_release_anchor(session: Session, tenant_id: int) -> datetime | None:
    setting = _setting(session, tenant_id)
    anchor = getattr(setting, "ai_group_continuity_release_anchor", None) if setting else None
    return ensure_aware(anchor) if isinstance(anchor, datetime) else None


def _setting(session: Session, tenant_id: int) -> SchedulingSetting | None:
    return session.scalar(
        select(SchedulingSetting).where(SchedulingSetting.tenant_id == tenant_id)
    ) or session.scalar(select(SchedulingSetting).where(SchedulingSetting.tenant_id.is_(None)))


__all__ = ["continuity_enabled", "continuity_release_anchor"]

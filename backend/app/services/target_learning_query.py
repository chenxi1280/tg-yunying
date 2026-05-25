from __future__ import annotations

from app.models import TargetLearningSample


def page_size(filters: dict) -> int:
    try:
        return max(1, min(200, int(filters.get("page_size") or 50)))
    except (TypeError, ValueError):
        return 50


def page_number(filters: dict) -> int:
    try:
        return max(1, int(filters.get("page") or 1))
    except (TypeError, ValueError):
        return 1


def sample_time_filter(stmt, filters: dict):
    if filters.get("sent_from"):
        stmt = stmt.where(TargetLearningSample.sent_at >= str(filters["sent_from"]))
    if filters.get("sent_to"):
        stmt = stmt.where(TargetLearningSample.sent_at <= str(filters["sent_to"]))
    return stmt

from __future__ import annotations

from typing import Any


def filter_voice_profile_rows(rows: list[dict[str, Any]], search: str) -> list[dict[str, Any]]:
    keyword = search.strip().lower()
    if not keyword:
        return rows
    return [row for row in rows if _row_matches(row, keyword)]


def _row_matches(row: dict[str, Any], keyword: str) -> bool:
    return any(keyword in value for value in _search_values(row))


def _search_values(row: dict[str, Any]) -> list[str]:
    updated_at = row.get("updated_at")
    return [
        str(row.get("display_name") or "").lower(),
        str(row.get("username") or "").lower(),
        str(row.get("phone_masked") or "").lower(),
        str(row.get("account_status") or "").lower(),
        str(row.get("profile_status") or "").lower(),
        str(updated_at or "").lower(),
    ]


__all__ = ["filter_voice_profile_rows"]

"""Server-owned revisioning for AI group hard-hourly obligations."""

from __future__ import annotations

from typing import Any

from app.models import Task


EPOCH_CONFIG_FIELDS = frozenset(
    {
        "target_operation_target_id",
        "target_reference_revision",
        "target_group_id",
        "hard_hourly_target_enabled",
        "hourly_min_messages",
        "hard_hourly_strategy",
    }
)


def increment_revision_for_continuity_change(
    task: Task,
    *,
    previous_config: dict[str, Any],
    previous_timezone: str,
) -> bool:
    """Advance only when an AI hard-hourly obligation definition changed."""
    if task.type != "group_ai_chat":
        return False
    current_config = task.type_config or {}
    config_changed = any(
        previous_config.get(field) != current_config.get(field)
        for field in EPOCH_CONFIG_FIELDS
    )
    if not config_changed and str(previous_timezone or "") == str(task.timezone or ""):
        return False
    task.config_revision = max(1, int(task.config_revision or 1)) + 1
    return True


__all__ = ["increment_revision_for_continuity_change"]

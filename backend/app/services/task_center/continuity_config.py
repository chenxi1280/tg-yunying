"""Server-owned revisioning for AI group daily obligations."""

from __future__ import annotations

from typing import Any

from app.models import Task


EPOCH_CONFIG_FIELDS = frozenset(
    {
        "target_operation_target_id",
        "target_reference_revision",
        "target_group_id",
        "daily_message_target",
    }
)
AI_CONTENT_REVISION_FIELDS = frozenset({
    "ai_content_route_v2_enabled",
    "ai_content_policy_version_id",
    "ai_content_allowed_routes",
    "ai_content_attestation_ids",
    "ai_content_policy_manifest_id",
    "ai_content_sampling_manifest_hash",
    "ai_content_max_cost_per_slot",
    "ai_content_daily_budget",
})


def increment_revision_for_continuity_change(
    task: Task,
    *,
    previous_config: dict[str, Any],
    previous_timezone: str,
) -> bool:
    """Advance only when an AI daily obligation definition changed."""
    current_config = task.type_config or {}
    ai_content_changed = any(
        previous_config.get(field) != current_config.get(field)
        for field in AI_CONTENT_REVISION_FIELDS
    )
    if task.type != "group_ai_chat" and not ai_content_changed:
        return False
    config_changed = any(
        previous_config.get(field) != current_config.get(field)
        for field in EPOCH_CONFIG_FIELDS
    )
    if not config_changed and not ai_content_changed and (
        str(previous_timezone or "") == str(task.timezone or "")
    ):
        return False
    task.config_revision = max(1, int(task.config_revision or 1)) + 1
    return True


__all__ = ["increment_revision_for_continuity_change"]

"""Server-owned revisioning for AI group daily obligations."""

from __future__ import annotations

from datetime import datetime
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
AI_CONTENT_REVISION_FIELDS = frozenset(
    {
        "ai_content_route_v2_enabled",
        "ai_content_policy_version_id",
        "ai_content_allowed_routes",
        "ai_content_attestation_ids",
        "ai_content_policy_manifest_id",
        "ai_content_sampling_manifest_hash",
        "ai_content_max_cost_per_slot",
        "ai_content_daily_budget",
    }
)
AI_GROUP_CONTENT_POLICY_FIELDS = frozenset(
    {
        "topic_directions",
        "topic_participation_rate",
        "topic_participation_rate_next",
        "topic_participation_rate_effective_date",
        "teacher_targets",
        "system_prompt_override",
        "slang_prompt_template_id",
        "slang_terms",
        "tone",
        "account_personas",
    }
)
CONTENT_POLICY_REVISION_FIELD = "_ai_group_content_policy_revision"
CONTENT_POLICY_META_FIELD = "_ai_group_content_policy_meta"
RATE_POLICY_FIELDS = frozenset(
    {
        "topic_participation_rate",
        "topic_participation_rate_next",
        "topic_participation_rate_effective_date",
    }
)


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
    if (
        not config_changed
        and not ai_content_changed
        and (str(previous_timezone or "") == str(task.timezone or ""))
    ):
        return False
    task.config_revision = max(1, int(task.config_revision or 1)) + 1
    return True


def increment_revision_for_content_policy_change(
    task: Task,
    *,
    previous_config: dict[str, Any],
    previous_revision: int,
    observed_at: datetime | None = None,
) -> bool:
    if task.type != "group_ai_chat":
        return False
    current = dict(task.type_config or {})
    changed_fields = _changed_content_policy_fields(previous_config, current)
    if not changed_fields:
        return False
    revision = content_policy_revision(previous_config, fallback=previous_revision) + 1
    current[CONTENT_POLICY_REVISION_FIELD] = revision
    current[CONTENT_POLICY_META_FIELD] = _updated_policy_meta(
        previous_config,
        current,
        changed_fields=changed_fields,
        revision=revision,
        observed_at=observed_at,
    )
    task.type_config = current
    return True


def content_policy_revision(config: dict[str, Any], *, fallback: int = 1) -> int:
    return max(1, int(config.get(CONTENT_POLICY_REVISION_FIELD) or fallback or 1))


def is_content_policy_only_change(
    task: Task,
    *,
    previous_config: dict[str, Any],
) -> bool:
    if task.type != "group_ai_chat":
        return False
    changed = {
        key
        for key in set(previous_config) | set(task.type_config or {})
        if not key.startswith("_ai_group_content_policy_")
        and previous_config.get(key) != (task.type_config or {}).get(key)
    }
    return bool(changed) and changed <= AI_GROUP_CONTENT_POLICY_FIELDS


def _changed_content_policy_fields(
    previous: dict[str, Any],
    current: dict[str, Any],
) -> set[str]:
    return {
        field
        for field in AI_GROUP_CONTENT_POLICY_FIELDS
        if previous.get(field) != current.get(field)
    }


def _updated_policy_meta(
    previous: dict[str, Any],
    current: dict[str, Any],
    *,
    changed_fields: set[str],
    revision: int,
    observed_at: datetime | None,
) -> dict[str, Any]:
    meta = dict(previous.get(CONTENT_POLICY_META_FIELD) or {})
    effective_at = observed_at.isoformat() if observed_at else ""
    if changed_fields & RATE_POLICY_FIELDS:
        meta["topic_participation_rate"] = _rate_policy_meta(
            previous,
            current,
            meta.get("topic_participation_rate"),
            revision,
            effective_at,
        )
    for field in ("topic_directions", "teacher_targets"):
        if field in changed_fields:
            meta[field] = {"revision": revision, "effective_at": effective_at}
    return meta


def _rate_policy_meta(
    previous: dict[str, Any],
    current: dict[str, Any],
    existing: Any,
    revision: int,
    effective_at: str,
) -> dict[str, Any]:
    entry = dict(existing or {})
    pending = current.get("topic_participation_rate_next")
    if pending is None:
        if previous.get("topic_participation_rate_next") is not None and current.get(
            "topic_participation_rate"
        ) == previous.get("topic_participation_rate_next"):
            return {
                "current_revision": int(entry.get("next_revision") or revision),
                "current_effective_at": str(
                    entry.get("next_effective_at") or effective_at
                ),
            }
        return {"current_revision": revision, "current_effective_at": effective_at}
    previous_pending = previous.get("topic_participation_rate_next")
    if previous_pending is not None and current.get(
        "topic_participation_rate"
    ) != previous.get("topic_participation_rate"):
        entry["current_revision"] = int(entry.get("next_revision") or revision)
        entry["current_effective_at"] = str(
            entry.get("next_effective_at") or effective_at
        )
    entry.setdefault("current_revision", max(1, revision - 1))
    entry.setdefault("current_effective_at", "")
    entry["next_revision"] = revision
    entry["next_effective_at"] = str(
        current.get("topic_participation_rate_effective_date") or ""
    )
    return entry


__all__ = [
    "AI_GROUP_CONTENT_POLICY_FIELDS",
    "CONTENT_POLICY_META_FIELD",
    "CONTENT_POLICY_REVISION_FIELD",
    "content_policy_revision",
    "increment_revision_for_content_policy_change",
    "increment_revision_for_continuity_change",
    "is_content_policy_only_change",
]

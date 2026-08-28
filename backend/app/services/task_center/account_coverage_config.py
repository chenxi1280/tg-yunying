from __future__ import annotations

from typing import Any

from .utils import as_int


LEGACY_AI_TARGET_FIELDS = frozenset(
    {
        "per_account_daily_min_messages",
        "per_account_daily_max_messages",
        "hard_hourly_target_enabled",
        "hourly_min_messages",
        "hard_hourly_strategy",
    }
)


def normalize_ai_daily_target(
    config: dict[str, Any],
    *,
    frozen_account_count: int,
) -> dict[str, Any]:
    normalized = dict(config or {})
    _ = frozen_account_count
    configured = as_int(normalized.get("daily_message_target"))
    normalized["daily_message_target"] = max(1, configured or 1)
    normalized["account_coverage_mode"] = "all_accounts_daily"
    for field in LEGACY_AI_TARGET_FIELDS:
        normalized.pop(field, None)
    return normalized


def apply_group_ai_account_coverage_defaults(
    task_type: str,
    config: dict[str, Any],
    account_config: dict[str, Any] | None,
) -> dict[str, Any]:
    del account_config
    if task_type not in {"group_ai_chat", "channel_view"}:
        return config
    if config.get("account_coverage_mode") == "all_accounts_daily":
        return config
    return {**config, "account_coverage_mode": "all_accounts_daily"}


__all__ = [
    "apply_group_ai_account_coverage_defaults",
    "normalize_ai_daily_target",
]

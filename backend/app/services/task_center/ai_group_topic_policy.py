from __future__ import annotations

from datetime import date, timedelta
from typing import Any


NEXT_RATE_FIELD = "topic_participation_rate_next"
NEXT_RATE_DATE_FIELD = "topic_participation_rate_effective_date"
CONTENT_POLICY_META_FIELD = "_ai_group_content_policy_meta"


def effective_topic_rate(config: dict[str, Any], task_day: date) -> float | None:
    next_rate = config.get(NEXT_RATE_FIELD)
    effective_date = _date_value(config.get(NEXT_RATE_DATE_FIELD))
    if next_rate is not None and effective_date is not None and task_day >= effective_date:
        return float(next_rate)
    current = config.get("topic_participation_rate")
    return float(current) if current is not None else None


def stage_topic_rate_update(
    current_config: dict[str, Any],
    requested_rate: float,
    *,
    task_status: str,
    today: date,
    has_current_task_day_plan: bool = False,
) -> dict[str, Any]:
    config = promote_due_topic_rate(current_config, today)
    current_rate = config.get("topic_participation_rate")
    current_day_frozen = task_status == "running" or has_current_task_day_plan
    if not current_day_frozen or current_rate is None:
        return _without_pending_rate({**config, "topic_participation_rate": requested_rate})
    if float(requested_rate) == float(current_rate):
        return _without_pending_rate(config)
    return {
        **config,
        NEXT_RATE_FIELD: requested_rate,
        NEXT_RATE_DATE_FIELD: (today + timedelta(days=1)).isoformat(),
    }


def promote_due_topic_rate(config: dict[str, Any], today: date) -> dict[str, Any]:
    effective_date = _date_value(config.get(NEXT_RATE_DATE_FIELD))
    if effective_date is None or effective_date > today or config.get(NEXT_RATE_FIELD) is None:
        return dict(config)
    promoted = {**config, "topic_participation_rate": config[NEXT_RATE_FIELD]}
    promoted = _promote_rate_metadata(promoted, effective_date)
    return _without_pending_rate(promoted)


def _promote_rate_metadata(config: dict[str, Any], effective_date: date) -> dict[str, Any]:
    policy_meta = dict(config.get(CONTENT_POLICY_META_FIELD) or {})
    rate_meta = dict(policy_meta.get("topic_participation_rate") or {})
    if not rate_meta:
        return config
    rate_meta = {
        "current_revision": int(
            rate_meta.get("next_revision") or rate_meta.get("current_revision") or 1
        ),
        "current_effective_at": str(
            rate_meta.get("next_effective_at") or effective_date.isoformat()
        ),
    }
    policy_meta["topic_participation_rate"] = rate_meta
    return {**config, CONTENT_POLICY_META_FIELD: policy_meta}


def _without_pending_rate(config: dict[str, Any]) -> dict[str, Any]:
    result = dict(config)
    result.pop(NEXT_RATE_FIELD, None)
    result.pop(NEXT_RATE_DATE_FIELD, None)
    return result


def _date_value(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if not value:
        return None
    return date.fromisoformat(str(value))


__all__ = [
    "NEXT_RATE_DATE_FIELD",
    "NEXT_RATE_FIELD",
    "effective_topic_rate",
    "promote_due_topic_rate",
    "stage_topic_rate_update",
]

from __future__ import annotations

from datetime import datetime

from app.models import Task

PENDING_KEY = "pending_search_click_revision"


def pending_search_click_revision(task: Task) -> dict:
    return dict((task.stats or {}).get(PENDING_KEY) or {})


def apply_pending_search_click_revision(
    task: Task,
    *,
    period_start: datetime,
) -> bool:
    pending = pending_search_click_revision(task)
    if not pending:
        return False
    effective_at = datetime.fromisoformat(str(pending["effective_at"]))
    comparable_period = _naive(period_start)
    if _naive(effective_at) > comparable_period:
        return False
    task.type_config = dict(pending["type_config"])
    task.account_config = dict(pending["account_config"])
    task.name = str(pending["name"])
    task.config_revision = int(pending["config_revision"])
    stats = dict(task.stats or {})
    stats.pop(PENDING_KEY, None)
    stats["applied_search_click_config_revision"] = task.config_revision
    task.stats = stats
    return True


def store_pending_search_click_revision(
    task: Task,
    *,
    effective_at: datetime,
    type_config: dict,
    account_config: dict,
    name: str,
) -> None:
    current = pending_search_click_revision(task)
    revision = int(current.get("config_revision") or task.config_revision + 1)
    stable_effective_at = str(
        current.get("effective_at") or effective_at.isoformat()
    )
    stats = dict(task.stats or {})
    stats[PENDING_KEY] = {
        "config_revision": revision,
        "effective_at": stable_effective_at,
        "type_config": type_config,
        "account_config": account_config,
        "name": name,
    }
    task.stats = stats


def _naive(value: datetime) -> datetime:
    return value.replace(tzinfo=None) if value.tzinfo else value


__all__ = [
    "apply_pending_search_click_revision",
    "pending_search_click_revision",
    "store_pending_search_click_revision",
]

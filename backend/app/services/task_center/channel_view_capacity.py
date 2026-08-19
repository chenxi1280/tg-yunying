from __future__ import annotations

from collections.abc import Iterable

from app.models import Task


UNIQUE_CAPACITY_STATS_KEY = "channel_view_unique_account_capacity_shortfall"


def record_unique_account_capacity(
    task: Task,
    source_capacities: Iterable[tuple[int, int]],
) -> bool:
    shortages = [
        (required, available)
        for required, available in source_capacities
        if required > available
    ]
    stats = dict(task.stats or {})
    if not shortages:
        stats.pop(UNIQUE_CAPACITY_STATS_KEY, None)
        task.stats = stats
        if task.last_error == UNIQUE_CAPACITY_STATS_KEY:
            task.last_error = ""
        return False
    required_count = sum(required for required, _available in shortages)
    available_count = sum(available for _required, available in shortages)
    stats[UNIQUE_CAPACITY_STATS_KEY] = {
        "source_count": len(shortages),
        "required_count": required_count,
        "available_count": available_count,
        "deficit_count": required_count - available_count,
    }
    task.stats = stats
    return True


__all__ = ["UNIQUE_CAPACITY_STATS_KEY", "record_unique_account_capacity"]

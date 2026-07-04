from __future__ import annotations

from app.models import Task


def runtime_search_join_config(task: Task) -> dict:
    type_config = dict(task.type_config or {})
    pacing_config = {key: value for key, value in dict(task.pacing_config or {}).items() if value is not None}
    return {**type_config, **pacing_config}


__all__ = ["runtime_search_join_config"]

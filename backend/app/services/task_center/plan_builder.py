from __future__ import annotations

from .executors import build_task_plan, reached_daily_action_limit


def build_plan(*args, **kwargs):
    """Compatibility shim; new task-center code should call executors.build_task_plan."""
    return build_task_plan(*args, **kwargs)


__all__ = ["build_plan", "build_task_plan", "reached_daily_action_limit"]

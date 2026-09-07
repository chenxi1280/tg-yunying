"""Allocate membership attempts inside a bounded window without compressing gaps."""
from datetime import timedelta
import random


DEFAULT_WINDOW_HOURS = 2
MIN_WINDOW_HOURS = 1
MAX_WINDOW_HOURS = 6
SECONDS_PER_HOUR = 3600
MIN_MEMBERSHIP_GAP_SECONDS = 15
JITTER_STEP_RATIO = 0.3


def membership_window_hours(config: dict) -> int:
    hours = config.get("membership_schedule_window_hours", DEFAULT_WINDOW_HOURS)
    if type(hours) is not int or not MIN_WINDOW_HOURS <= hours <= MAX_WINDOW_HOURS:
        raise ValueError("membership_schedule_window_hours must be an integer from 1 to 6")
    return hours


def channel_membership_schedule(task, pending_count: int, now_value) -> list:
    window = membership_window_hours(task.type_config or {}) * SECONDS_PER_HOUR
    if pending_count <= 0:
        return []
    if pending_count == 1:
        return [now_value]
    if (pending_count - 1) * MIN_MEMBERSHIP_GAP_SECONDS > window:
        raise ValueError(
            f"membership_schedule_capacity_exceeded: {pending_count} accounts, {window} seconds"
        )
    step = window / (pending_count - 1)
    jitter = min(step * JITTER_STEP_RATIO, (step - MIN_MEMBERSHIP_GAP_SECONDS) / 2)
    return [
        now_value + timedelta(seconds=step * index + random.uniform(
            0 if index == 0 else -jitter,
            0 if index == pending_count - 1 else jitter,
        ))
        for index in range(pending_count)
    ]

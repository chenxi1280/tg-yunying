"""Freeze a humanized 10–24 hour membership schedule without compressing gaps."""
from datetime import timedelta
import random


MIN_WINDOW_HOURS = 10
MAX_WINDOW_HOURS = 24
SECONDS_PER_HOUR = 3600
MIN_MEMBERSHIP_GAP_SECONDS = 15
JITTER_STEP_RATIO = 0.3


def channel_membership_schedule(pending_count: int, now_value) -> list:
    if pending_count <= 0:
        return []
    if pending_count == 1:
        return [now_value]
    maximum_window = MAX_WINDOW_HOURS * SECONDS_PER_HOUR
    minimum_window = max(MIN_WINDOW_HOURS * SECONDS_PER_HOUR, (pending_count - 1) * MIN_MEMBERSHIP_GAP_SECONDS)
    if minimum_window > maximum_window:
        raise ValueError(
            f"membership_schedule_capacity_exceeded: {pending_count} accounts, {maximum_window} seconds"
        )
    window = random.randint(minimum_window, maximum_window)
    step = window // (pending_count - 1)
    jitter = min(int(step * JITTER_STEP_RATIO), (step - MIN_MEMBERSHIP_GAP_SECONDS) // 2)
    return [
        now_value + timedelta(seconds=window * index // (pending_count - 1) + (
            0 if index in {0, pending_count - 1} else random.randint(-jitter, jitter)
        ))
        for index in range(pending_count)
    ]

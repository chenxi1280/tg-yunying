from __future__ import annotations

from math import ceil
from typing import Any


AI_GROUP_HOURLY_PER_ACCOUNT = 6
AI_GROUP_MIN_HOURLY = 20
AI_GROUP_MAX_HOURLY = 120
AI_GROUP_DEFAULT_ROUNDS_PER_HOUR = 6

AI_COMMENT_HOURLY_PER_ACCOUNT = 4
AI_COMMENT_MIN_HOURLY = 20
AI_COMMENT_MAX_HOURLY = 150
AI_COMMENT_TARGET_RATIO = 0.6
AI_COMMENT_MIN_PER_MESSAGE = 10
AI_COMMENT_MAX_PER_MESSAGE = 80
AI_COMMENT_MIN_PER_ACCOUNT_HOUR = 4
AI_COMMENT_MAX_PER_ACCOUNT_HOUR = 6


def clamp_int(value: int, lower: int, upper: int) -> int:
    return max(lower, min(upper, int(value)))


def recommend_ai_limits(task_type: str, ready_account_count: int, *, current_hour_rounds: int = AI_GROUP_DEFAULT_ROUNDS_PER_HOUR) -> dict[str, Any]:
    account_count = max(0, int(ready_account_count or 0))
    if task_type == "group_ai_chat":
        hourly = _recommended_group_hourly(account_count)
        rounds = max(1, int(current_hour_rounds or AI_GROUP_DEFAULT_ROUNDS_PER_HOUR))
        return {
            "max_actions_per_hour": hourly,
            "messages_per_round": max(1, ceil(hourly / rounds)),
            "current_hour_rounds": rounds,
            "estimated_hourly_capacity": min(hourly, rounds * max(1, ceil(hourly / rounds))),
            "basis": {"ready_account_count": account_count, "hourly_per_account": AI_GROUP_HOURLY_PER_ACCOUNT, "hourly_rounds": rounds},
        }
    if task_type == "channel_comment":
        hourly = _recommended_comment_hourly(account_count)
        return {
            "max_actions_per_hour": hourly,
            "target_comments_per_message": _recommended_comment_target(account_count),
            "max_comments_per_account_per_hour": _recommended_comment_per_account(hourly, account_count),
            "basis": {"ready_account_count": account_count, "hourly_per_account": AI_COMMENT_HOURLY_PER_ACCOUNT},
        }
    return {}


def _recommended_group_hourly(account_count: int) -> int:
    return clamp_int(max(1, account_count) * AI_GROUP_HOURLY_PER_ACCOUNT, AI_GROUP_MIN_HOURLY, AI_GROUP_MAX_HOURLY)


def _recommended_comment_hourly(account_count: int) -> int:
    return clamp_int(max(1, account_count) * AI_COMMENT_HOURLY_PER_ACCOUNT, AI_COMMENT_MIN_HOURLY, AI_COMMENT_MAX_HOURLY)


def _recommended_comment_target(account_count: int) -> int:
    return clamp_int(round(max(1, account_count) * AI_COMMENT_TARGET_RATIO), AI_COMMENT_MIN_PER_MESSAGE, AI_COMMENT_MAX_PER_MESSAGE)


def _recommended_comment_per_account(hourly: int, account_count: int) -> int:
    if account_count <= 0:
        return AI_COMMENT_MIN_PER_ACCOUNT_HOUR
    return clamp_int(ceil(hourly / account_count), AI_COMMENT_MIN_PER_ACCOUNT_HOUR, AI_COMMENT_MAX_PER_ACCOUNT_HOUR)


def allocate_message_budget(deficits: list[int], budget: int) -> list[int]:
    allocations = [0 for _item in deficits]
    remaining = max(0, int(budget or 0))
    while remaining > 0:
        progressed = False
        for index, deficit in enumerate(deficits):
            if remaining <= 0:
                break
            if allocations[index] >= max(0, int(deficit or 0)):
                continue
            allocations[index] += 1
            remaining -= 1
            progressed = True
        if not progressed:
            break
    return allocations

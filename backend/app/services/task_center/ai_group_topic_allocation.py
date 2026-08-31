"""Topic allocation, 30% hard cap formula, and remote topic capacity reservation."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any


MAX_TOPIC_PARTICIPATION_RATE = 0.30
MAX_TOPIC_RATE_BPS = 3000
MIN_TOPIC_RATE_BPS = 0


@dataclass(frozen=True)
class TopicAllocationDecision:
    topic_mode: str  # configured_topic | human_context | group_free_chat
    is_eligible_by_ordinal: bool
    has_remote_capacity: bool
    topic_direction: dict[str, Any] | None


def normalize_topic_participation_rate(rate: float | int | Decimal | None) -> int:
    if rate is None:
        raise ValueError("topic_participation_rate_required")
    if isinstance(rate, bool) or not isinstance(rate, (int, float, Decimal)):
        raise ValueError("topic_participation_rate_must_be_numeric")
    try:
        value = Decimal(str(rate))
    except (InvalidOperation, ValueError):
        raise ValueError("topic_participation_rate_must_be_numeric")
    if not value.is_finite() or value < 0 or value > Decimal("0.30"):
        raise ValueError("topic_participation_rate_out_of_range_0_to_0_30")
    if value.as_tuple().exponent < -2:
        raise ValueError("topic_participation_rate_max_two_decimal_places")
    return int(value * 10000)


def is_ordinal_topic_eligible(ordinal: int, topic_rate_bps: int) -> bool:
    if ordinal <= 0 or topic_rate_bps <= 0:
        return False
    return (ordinal * topic_rate_bps) // 10000 > (
        (ordinal - 1) * topic_rate_bps
    ) // 10000


def check_remote_topic_capacity(
    *,
    confirmed_normal_count: int,
    confirmed_topic_count: int,
    unknown_topic_count: int = 0,
    active_reservations: int = 0,
    active_normal_count: int | None = None,
    topic_rate_bps: int,
) -> bool:
    if topic_rate_bps <= 0:
        return False
    active_normal = (
        active_reservations if active_normal_count is None else active_normal_count
    )
    num = confirmed_topic_count + unknown_topic_count + active_reservations + 1
    den = confirmed_normal_count + unknown_topic_count + active_normal + 1
    return num * 10000 <= den * topic_rate_bps


def decide_topic_mode(
    *,
    normal_text_ordinal: int,
    topic_rate_bps: int,
    has_configured_topics: bool,
    has_human_context: bool,
    confirmed_normal_count: int,
    confirmed_topic_count: int,
    unknown_topic_count: int = 0,
    active_reservations: int = 0,
    active_normal_count: int | None = None,
    chosen_topic_direction: dict[str, Any] | None = None,
) -> TopicAllocationDecision:
    if has_human_context:
        return TopicAllocationDecision("human_context", False, False, None)

    eligible = is_ordinal_topic_eligible(normal_text_ordinal, topic_rate_bps)
    if not eligible or not has_configured_topics:
        return TopicAllocationDecision("group_free_chat", eligible, False, None)

    has_capacity = check_remote_topic_capacity(
        confirmed_normal_count=confirmed_normal_count,
        confirmed_topic_count=confirmed_topic_count,
        unknown_topic_count=unknown_topic_count,
        active_reservations=active_reservations,
        active_normal_count=active_normal_count,
        topic_rate_bps=topic_rate_bps,
    )
    if not has_capacity:
        return TopicAllocationDecision("group_free_chat", True, False, None)

    return TopicAllocationDecision(
        "configured_topic", True, True, chosen_topic_direction
    )


def validate_no_consecutive_three_questions(recent_act_types: list[str]) -> bool:
    return not (
        len(recent_act_types) >= 3
        and all(a == "question" for a in recent_act_types[-3:])
    )


def validate_question_ratio_in_window(
    recent_act_types: list[str], max_ratio: float = 0.40
) -> bool:
    window = recent_act_types[-10:] if len(recent_act_types) >= 10 else recent_act_types
    if len(window) < 10:
        return True
    return sum(1 for a in window if a == "question") <= int(len(window) * max_ratio)


__all__ = [
    "MAX_TOPIC_PARTICIPATION_RATE",
    "MAX_TOPIC_RATE_BPS",
    "TopicAllocationDecision",
    "check_remote_topic_capacity",
    "decide_topic_mode",
    "is_ordinal_topic_eligible",
    "normalize_topic_participation_rate",
    "validate_no_consecutive_three_questions",
    "validate_question_ratio_in_window",
]

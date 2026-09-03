from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timedelta


HourSpan = tuple[datetime, datetime, float]
DueBucket = tuple[datetime, datetime, int]


def stable_slot_due_times(
    *,
    plan_total: int,
    slot_keys: list[str],
    slot_ordinals: list[int],
    period_start_at: datetime,
    deadline_at: datetime,
    hourly_curve: list[int],
    seed_id: str,
    multi_day_rampup: bool = False,
) -> list[datetime]:
    """Return period-stable due times for an arbitrary subset of a frozen plan."""
    _validate_slots(plan_total, slot_keys, slot_ordinals)
    buckets = stratified_hour_buckets(
        plan_total,
        hourly_curve,
        period_start_at,
        deadline_at,
        multi_day_rampup=multi_day_rampup,
    )
    if not buckets:
        return []
    assignments = _ordinal_assignments(buckets)
    return [
        _stratum_due_time(*assignments[ordinal], seed_id, slot_key)
        for slot_key, ordinal in zip(slot_keys, slot_ordinals, strict=True)
    ]


def pacing_plan_hash(
    *,
    plan_total: int,
    period_start_at: datetime,
    deadline_at: datetime,
    hourly_curve: list[int],
    seed_id: str,
) -> str:
    payload = [
        plan_total,
        period_start_at.isoformat(),
        deadline_at.isoformat(),
        hourly_curve,
        seed_id,
    ]
    canonical = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def stratified_hour_buckets(
    plan_total: int,
    hourly_curve: list[int],
    period_start_at: datetime,
    deadline_at: datetime,
    *,
    multi_day_rampup: bool = False,
) -> list[DueBucket]:
    total_seconds = (deadline_at - period_start_at).total_seconds()
    if not multi_day_rampup or total_seconds <= 86400 * 1.5:
        spans = _weighted_hour_spans(hourly_curve, period_start_at, deadline_at)
        if not spans:
            return []
        counts = _largest_remainder_counts(plan_total, [span[2] for span in spans])
        return [
            (start, end, count)
            for (start, end, _weight), count in zip(spans, counts, strict=True)
            if count > 0
        ]
    # 多日阶梯爬坡分桶（日风控翻倍）：先分发每日配额，再分发到具体小时
    day_spans: list[tuple[datetime, datetime]] = []
    day_cursor = period_start_at
    while day_cursor < deadline_at:
        day_boundary = min(day_cursor + timedelta(days=1), deadline_at)
        day_spans.append((day_cursor, day_boundary))
        day_cursor = day_boundary
    day_weights = [
        _daily_ramp_factor(s, period_start_at, deadline_at) * (e - s).total_seconds()
        for s, e in day_spans
    ]
    day_counts = _largest_remainder_counts(plan_total, day_weights)
    result_buckets: list[DueBucket] = []
    for (day_start, day_end), count_for_day in zip(day_spans, day_counts, strict=True):
        if count_for_day <= 0:
            continue
        hour_spans = _weighted_hour_spans(hourly_curve, day_start, day_end)
        if not hour_spans:
            continue
        hour_counts = _largest_remainder_counts(count_for_day, [span[2] for span in hour_spans])
        for (h_start, h_end, _w), h_cnt in zip(hour_spans, hour_counts, strict=True):
            if h_cnt > 0:
                result_buckets.append((h_start, h_end, h_cnt))
    return result_buckets


def _validate_slots(plan_total: int, slot_keys: list[str], slot_ordinals: list[int]) -> None:
    if plan_total <= 0:
        raise ValueError("pacing_plan_total_must_be_positive")
    if len(slot_keys) != len(slot_ordinals):
        raise ValueError("pacing_slot_identity_length_mismatch")
    if len(set(slot_ordinals)) != len(slot_ordinals):
        raise ValueError("pacing_slot_ordinal_duplicate")
    if any(ordinal < 0 or ordinal >= plan_total for ordinal in slot_ordinals):
        raise ValueError("pacing_slot_ordinal_out_of_plan")


def _daily_ramp_factor(cursor: datetime, start_at: datetime, deadline_at: datetime) -> float:
    total_seconds = (deadline_at - start_at).total_seconds()
    if total_seconds <= 86400 * 1.5:
        return 1.0
    day_index = max(0, int((cursor - start_at).total_seconds() // 86400))
    # 3~5 天日风控翻倍与阶梯放量模型:
    # Day 0 (0~24h): 1.0 首日低频试探 (~16%)
    # Day 1 (24~48h): 2.0 次日翻倍放量 (~33%)
    # Day 2 (48~72h): 3.0 峰值释放 (~50%)
    # Day 3 (72~96h): 2.5
    # Day 4 (96~120h): 1.5
    ramp_factors = [1.0, 2.0, 3.0, 2.5, 1.5]
    return ramp_factors[day_index] if day_index < len(ramp_factors) else 1.0


def _weighted_hour_spans(
    hourly_curve: list[int],
    start_at: datetime,
    deadline_at: datetime,
) -> list[HourSpan]:
    spans: list[HourSpan] = []
    cursor = start_at
    while cursor < deadline_at:
        boundary = min(
            cursor.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1),
            deadline_at,
        )
        weight = 1 if not hourly_curve else hourly_curve[cursor.hour]
        if weight > 0:
            seconds = (boundary - cursor).total_seconds()
            spans.append((cursor, boundary, float(weight) * seconds))
        cursor = boundary
    return spans


def _largest_remainder_counts(total: int, weights: list[float]) -> list[int]:
    weight_total = sum(weights)
    shares = [total * weight / weight_total for weight in weights]
    counts = [math.floor(share) for share in shares]
    remainders = [share - count for share, count in zip(shares, counts, strict=True)]
    missing = total - sum(counts)
    order = sorted(range(len(weights)), key=lambda index: (-remainders[index], index))
    for index in order[:missing]:
        counts[index] += 1
    return counts


def _ordinal_assignments(buckets: list[DueBucket]) -> dict[int, DueBucket]:
    assignments: dict[int, DueBucket] = {}
    ordinal = 0
    for start, end, count in buckets:
        for stratum in range(count):
            assignments[ordinal] = (start, end, count, stratum)  # type: ignore[assignment]
            ordinal += 1
    return assignments


def _stratum_due_time(
    bucket_start: datetime,
    bucket_end: datetime,
    count: int,
    stratum: int,
    seed_id: str,
    slot_key: str,
) -> datetime:
    span = max(1.0, (bucket_end - bucket_start).total_seconds())
    width = span / count
    ratio = _deterministic_offset_ratio(seed_id, slot_key, stratum)
    return bucket_start + timedelta(seconds=stratum * width + ratio * width)


def _deterministic_offset_ratio(seed_id: str, slot_key: str, stratum: int) -> float:
    canonical = json.dumps(
        [seed_id, slot_key, stratum],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") / float(2**64)


__all__ = ["pacing_plan_hash", "stable_slot_due_times", "stratified_hour_buckets"]

from __future__ import annotations

from datetime import datetime, timedelta


def curve_schedule_times(
    total_actions: int,
    curve: list[int],
    max_per_hour: int,
    start_at: datetime,
) -> list[datetime]:
    total_weight = sum(curve)
    if total_weight <= 0:
        return []
    slots: list[tuple[int, int, float]] = []
    for offset in range(24):
        hour = (start_at.hour + offset) % 24
        weight = curve[hour]
        if weight <= 0:
            continue
        exact = total_actions * (weight / total_weight)
        count = min(int(exact), max_per_hour) if max_per_hour else int(exact)
        slots.append((offset, count, exact - count))
    remaining = _remaining_slot_count(total_actions, slots, max_per_hour)
    slots = _assign_fractional_slots(slots, remaining, max_per_hour)
    result: list[datetime] = []
    for offset, count, _fraction in sorted(slots, key=lambda item: item[0]):
        result.extend(_hour_schedule(start_at, offset, count))
    return sorted(result)[:total_actions]


def _remaining_slot_count(
    total_actions: int,
    slots: list[tuple[int, int, float]],
    max_per_hour: int,
) -> int:
    assigned = sum(count for _offset, count, _fraction in slots)
    capacity = sum((max_per_hour or total_actions) - count for _offset, count, _fraction in slots)
    return min(total_actions - assigned, max(0, capacity))


def _assign_fractional_slots(
    slots: list[tuple[int, int, float]],
    remaining: int,
    max_per_hour: int,
) -> list[tuple[int, int, float]]:
    assigned: list[tuple[int, int, float]] = []
    for offset, count, fraction in sorted(slots, key=lambda item: item[2], reverse=True):
        if remaining > 0 and (not max_per_hour or count < max_per_hour):
            count += 1
            remaining -= 1
        assigned.append((offset, count, fraction))
    return assigned


def _hour_schedule(start_at: datetime, offset: int, count: int) -> list[datetime]:
    if count <= 0:
        return []
    hour_start = start_at.replace(minute=0, second=0, microsecond=0) + timedelta(hours=offset)
    if offset == 0:
        hour_start = max(start_at, hour_start)
    hour_end = hour_start.replace(minute=59, second=59, microsecond=0)
    available_seconds = max(1, int((hour_end - hour_start).total_seconds()))
    step = max(1, available_seconds // count)
    return [
        hour_start + timedelta(seconds=min(available_seconds, index * step))
        for index in range(count)
    ]


__all__ = ["curve_schedule_times"]

"""Place unbound frozen obligations in their original source window's gaps."""
from datetime import timedelta

from .source_pacing import SourcePacingPoint, _recovery_jitter, wall_datetime

RECOVERY_INITIAL_GAP_FRACTION = 0.2
MINIMUM_SOURCE_GAP_SECONDS = 1.0


def recover_frozen_source_points(slots, due_by_slot, *, now_at, seed_id, other_points):
    result = dict(other_points)
    occupied = _history_points(slots)
    by_key = {slot.slot_key: slot for slot in slots}
    for key, point in other_points.items():
        occupied.append((point.release_not_before_at, _gap(by_key[key])))
    recoverable = [slot for slot in slots if slot.recovery_source_history is not None]
    overdue = []
    for slot in recoverable:
        release = max(wall_datetime(due_by_slot[slot.slot_key]),
                      wall_datetime(slot.release_not_before_at))
        if release >= wall_datetime(slot.deadline_at):
            continue
        if release > now_at:
            result[slot.slot_key] = SourcePacingPoint(due_by_slot[slot.slot_key], release)
            occupied.append((release, _gap(slot)))
        else:
            overdue.append(slot)
    for slot in sorted(overdue, key=lambda item: (due_by_slot[item.slot_key], item.slot_ordinal, item.slot_key)):
        gap = _gap(slot)
        jitter = _recovery_jitter(seed_id, slot.slot_key, gap)
        start = max(now_at + timedelta(seconds=gap * RECOVERY_INITIAL_GAP_FRACTION + jitter),
                    wall_datetime(slot.period_start_at), wall_datetime(slot.release_not_before_at),
                    wall_datetime(due_by_slot[slot.slot_key]))
        release = _available_point(start, occupied, gap=gap, jitter=jitter)
        if release >= wall_datetime(slot.deadline_at):
            continue
        result[slot.slot_key] = SourcePacingPoint(due_by_slot[slot.slot_key], release)
        occupied.append((release, gap))
    return result


def _history_points(slots):
    points = set()
    for slot in slots:
        if slot.recovery_source_history is None:
            continue
        start, end = wall_datetime(slot.period_start_at), wall_datetime(slot.deadline_at)
        for at, total in slot.recovery_source_history:
            when = wall_datetime(at)
            if start <= when < end:
                points.add((when, _period_gap(start, end, total)))
    return list(points)


def _gap(slot):
    return _period_gap(wall_datetime(slot.period_start_at), wall_datetime(slot.deadline_at), slot.plan_total)


def _period_gap(start, end, total):
    if total <= 0:
        raise ValueError('pacing_source_history_plan_total_invalid')
    return max(MINIMUM_SOURCE_GAP_SECONDS, (end - start).total_seconds() / total)


def _available_point(start, occupied, *, gap, jitter):
    candidate = start
    for at, other_gap in sorted(occupied):
        spacing = timedelta(seconds=max(gap, other_gap))
        if candidate <= at - spacing:
            break
        if candidate < at + spacing:
            candidate = at + spacing + timedelta(seconds=jitter)
    return candidate

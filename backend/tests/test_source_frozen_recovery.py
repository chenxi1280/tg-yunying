from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from app.services.task_center.source_pacing import SourcePacingSlot, schedule_source_pacing_points

pytestmark = pytest.mark.no_postgres
NOW = datetime(2026, 9, 11, 8)
GAP = timedelta(minutes=10)
DEADLINE = NOW + timedelta(hours=1)


def _slot(key='overdue', **changes):
    slot = SourcePacingSlot(source_key='message', slot_key=key, slot_ordinal=0, plan_total=12,
        period_start_at=NOW-timedelta(hours=1), deadline_at=DEADLINE,
        frozen_due_at=NOW-GAP, release_not_before_at=NOW-GAP, owner_id=key,
        historical_cursor_at=DEADLINE, historical_max_ordinal=11,
        recovery_source_history=((DEADLINE, 12), (NOW+GAP*5, 12)))
    return replace(slot, **changes)


def _plan(slots):
    return schedule_source_pacing_points(slots, {}, now_at=NOW, seed_id='frozen')


def test_overdue_owner_uses_gap_before_future_tail_without_changing_due():
    slot = _slot()
    point = _plan([slot])[slot.slot_key]
    assert point.due_at == NOW-GAP
    assert NOW < point.release_not_before_at < NOW+GAP
    assert slot.release_not_before_at == NOW-GAP


def test_same_batch_respects_both_sides_and_keeps_future_frozen_point():
    future = _slot('future', release_not_before_at=NOW+GAP*2)
    slots = [_slot('first'), _slot('second', slot_ordinal=1), future]
    points = _plan(slots)
    times = sorted([point.release_not_before_at for point in points.values()] + [NOW+GAP*5])
    assert len(points) == 3
    assert points['future'].release_not_before_at == NOW+GAP*2
    assert all(right-left >= GAP for left, right in zip(times, times[1:]))


def test_deadline_owner_does_not_hide_other_overdue_owner():
    points = _plan([_slot('closed', release_not_before_at=DEADLINE), _slot('eligible')])
    assert set(points) == {'eligible'}


def test_replanning_future_recovery_point_is_stable():
    slot = _slot()
    first = _plan([slot])[slot.slot_key]
    persisted = replace(slot, release_not_before_at=first.release_not_before_at)
    assert _plan([persisted])[slot.slot_key] == first


def test_larger_neighbor_gap_and_no_space_are_preserved():
    slot = _slot(recovery_source_history=((NOW, 4), (NOW+GAP*4, 4)))
    assert _plan([slot]) == {}


def test_unfrozen_ordinal_still_uses_historical_cursor():
    slot = _slot(frozen_due_at=None, release_not_before_at=None, slot_ordinal=12,
                 plan_total=13, recovery_source_history=None)
    assert _plan([slot]) == {}

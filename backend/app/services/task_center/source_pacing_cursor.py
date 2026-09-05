"""Source row lock owner allocates stable slots for new or displaced admissions."""
from datetime import datetime, timedelta

from app.models import Action, SourcePacingAdmission, SourcePacingState

from .source_pacing import wall_datetime
from .source_pacing_recovery import late_admission_not_before
from .source_pacing_reservation import SourceAdmissionSpec


def admission_not_before(
    action: Action,
    state: SourcePacingState,
    *,
    admission: SourcePacingAdmission,
    spec: SourceAdmissionSpec,
    created: bool,
    timestamp: datetime,
) -> datetime:
    previous = wall_datetime(admission.call_not_before_at)
    not_before = _call_not_before(action, state, spec) if created else _reused_not_before(
        state,
        admission=admission,
        spec=spec,
    )
    if created and not_before <= timestamp and action.status not in {
        "failed",
        "retryable_failed",
    }:
        recovery_at = late_admission_not_before(
            action_id=action.id,
            release_at=spec.release_at,
            now_at=timestamp,
            gap_seconds=spec.source_gap_seconds,
            deadline_at=spec.deadline_at,
        )
        not_before = max(not_before, recovery_at)
    admission.call_not_before_at = not_before
    if created or not_before > previous:
        state.next_call_not_before_at = not_before + timedelta(
            seconds=spec.source_gap_seconds
        )
    return not_before


def _call_not_before(
    action: Action,
    state: SourcePacingState,
    spec: SourceAdmissionSpec,
) -> datetime:
    candidates = [spec.release_at]
    if action.effective_claim_at is not None:
        candidates.append(wall_datetime(action.effective_claim_at))
    if state.next_call_not_before_at is not None:
        candidates.append(wall_datetime(state.next_call_not_before_at))
    if state.last_call_started_at is not None:
        gap = max(int(state.last_source_gap_seconds or 0), spec.source_gap_seconds)
        candidates.append(wall_datetime(state.last_call_started_at) + timedelta(seconds=gap))
    return max(candidates)


def _reused_not_before(
    state: SourcePacingState,
    *,
    admission: SourcePacingAdmission,
    spec: SourceAdmissionSpec,
) -> datetime:
    reserved_at = wall_datetime(admission.call_not_before_at)
    if state.last_call_started_at is None:
        return reserved_at
    gap = max(int(state.last_source_gap_seconds or 0), spec.source_gap_seconds)
    after_call = wall_datetime(state.last_call_started_at) + timedelta(seconds=gap)
    if after_call <= reserved_at:
        return reserved_at
    # A displaced reservation needs its own slot, not the shared last-call edge.
    next_slot = wall_datetime(state.next_call_not_before_at) if state.next_call_not_before_at else after_call
    return max(after_call, next_slot)


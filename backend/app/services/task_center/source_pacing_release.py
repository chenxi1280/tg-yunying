from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import Action, ExecutionAttempt, SourcePacingAdmission, SourcePacingState

from .direct_action_claims import reconcile_source_pacing_states


def release_source_pacing_admissions_before_gateway(
    session: Session,
    action: Action,
) -> None:
    state_ids = set(session.scalars(
        select(SourcePacingAdmission.source_pacing_state_id).where(
            SourcePacingAdmission.action_id == action.id,
            SourcePacingAdmission.state.in_(("reserved", "finished")),
        )
    ))
    if not state_ids:
        return
    list(session.scalars(
        select(SourcePacingState)
        .where(SourcePacingState.id.in_(state_ids))
        .order_by(SourcePacingState.id)
        .with_for_update()
    ))
    admissions = session.scalars(
        select(SourcePacingAdmission)
        .outerjoin(ExecutionAttempt, ExecutionAttempt.id == SourcePacingAdmission.attempt_id)
        .where(
            SourcePacingAdmission.action_id == action.id,
            SourcePacingAdmission.state.in_(("reserved", "finished")),
            or_(
                SourcePacingAdmission.attempt_id.is_(None),
                ExecutionAttempt.gateway_call_started_at.is_(None),
            ),
        )
        .with_for_update(of=SourcePacingAdmission)
    )
    cancelled = set()
    for admission in admissions:
        admission.state = "cancelled_pre_gateway"
        admission.version = int(admission.version or 1) + 1
        cancelled.add(admission.source_pacing_state_id)
    reconcile_source_pacing_states(session, cancelled)


__all__ = ["release_source_pacing_admissions_before_gateway"]

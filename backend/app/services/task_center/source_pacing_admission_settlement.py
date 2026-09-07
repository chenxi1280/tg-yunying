from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, object_session

from app.models import Action, ExecutionAttempt, SourcePacingAdmission


def unsettled_prior_admission(
    session: Session,
    action: Action,
) -> SourcePacingAdmission | None:
    session.flush()
    return session.scalar(
        select(SourcePacingAdmission)
        .where(
            SourcePacingAdmission.action_id == action.id,
            SourcePacingAdmission.state.in_(("call_started", "remote_unknown")),
        )
        .order_by(SourcePacingAdmission.created_at.desc())
        .with_for_update()
        .limit(1)
    )


def finished_before_gateway(
    session: Session,
    admission: SourcePacingAdmission,
) -> bool:
    if admission.state != "finished" or not admission.attempt_id:
        return False
    attempt = session.get(ExecutionAttempt, admission.attempt_id)
    return attempt is not None and attempt.gateway_call_started_at is None


def settle_source_pacing_admission(
    action: Action,
    attempt: ExecutionAttempt | None,
) -> None:
    session = object_session(action)
    if session is None or attempt is None:
        return
    session.flush()
    admission = session.scalar(select(SourcePacingAdmission).where(
        SourcePacingAdmission.action_id == action.id,
        SourcePacingAdmission.attempt_id == attempt.id,
        SourcePacingAdmission.state == "call_started",
    ))
    if admission is None:
        return
    is_unknown = (
        action.status == "unknown_after_send"
        or attempt.status == "result_unknown"
    )
    admission.state = "remote_unknown" if is_unknown else "finished"
    admission.version = int(admission.version or 1) + 1


__all__ = [
    "finished_before_gateway",
    "settle_source_pacing_admission",
    "unsettled_prior_admission",
]

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
from uuid import uuid4

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, aliased

from app.models import (
    Action,
    ExecutionAttempt,
    SourcePacingAdmission,
    SourcePacingState,
)

from .source_pacing_admission_settlement import finished_before_gateway


SAFE_BOUND_ACTION_STATUSES = (
    "pending",
    "claiming",
    "executing",
    "retryable_failed",
    "failed",
    "skipped",
)


@dataclass(frozen=True)
class SourceAdmissionSpec:
    pacing_domain: str
    source_key_hash: str
    owner_type: str
    owner_id: str
    lifecycle_epoch: int
    period_key: str
    plan_hash: str
    release_at: datetime
    deadline_at: datetime
    source_gap_seconds: int


def lock_or_create_admission(
    session: Session,
    action: Action,
    *,
    attempt: ExecutionAttempt,
    state: SourcePacingState,
    spec: SourceAdmissionSpec,
) -> tuple[SourcePacingAdmission, bool]:
    reusable = _lock_reusable_owner_admission(session, action, state=state, spec=spec)
    if reusable is not None:
        _bind_admission(reusable, action=action, attempt=attempt)
        return reusable, False
    key = _admission_key(session, action)
    inserted_id = _insert_admission(
        session,
        action,
        attempt=attempt,
        state=state,
        spec=spec,
        key=key,
    )
    admission = _lock_admission_by_key(session, key)
    if admission.source_pacing_state_id != state.id:
        raise ValueError("pacing_source_admission_conflict")
    recycled = finished_before_gateway(session, admission)
    if recycled:
        _bind_admission(admission, action=action, attempt=attempt)
    return admission, inserted_id is not None or recycled


def _lock_reusable_owner_admission(
    session: Session,
    action: Action,
    *,
    state: SourcePacingState,
    spec: SourceAdmissionSpec,
) -> SourcePacingAdmission | None:
    bound_action = aliased(Action)
    linked_attempt = aliased(ExecutionAttempt)
    safe_attempt = or_(
        SourcePacingAdmission.attempt_id.is_(None),
        linked_attempt.gateway_call_started_at.is_(None),
    )
    reusable_state = or_(
        SourcePacingAdmission.state == "reserved",
        and_(
            SourcePacingAdmission.state == "finished",
            SourcePacingAdmission.attempt_id.is_not(None),
        ),
    )
    return session.scalar(
        select(SourcePacingAdmission)
        .join(bound_action, bound_action.id == SourcePacingAdmission.action_id)
        .outerjoin(linked_attempt, linked_attempt.id == SourcePacingAdmission.attempt_id)
        .where(
            SourcePacingAdmission.tenant_id == action.tenant_id,
            SourcePacingAdmission.source_pacing_state_id == state.id,
            SourcePacingAdmission.owner_type == spec.owner_type,
            SourcePacingAdmission.owner_id == spec.owner_id,
            SourcePacingAdmission.lifecycle_epoch == spec.lifecycle_epoch,
            SourcePacingAdmission.pacing_period_key == spec.period_key,
            SourcePacingAdmission.pacing_plan_hash == spec.plan_hash,
            bound_action.status.in_(SAFE_BOUND_ACTION_STATUSES),
            reusable_state,
            safe_attempt,
        )
        .order_by(
            case(
                (SourcePacingAdmission.action_id == action.id, 0),
                else_=1,
            ),
            SourcePacingAdmission.call_not_before_at.asc(),
            SourcePacingAdmission.created_at.asc(),
            SourcePacingAdmission.id.asc(),
        )
        .with_for_update(of=SourcePacingAdmission)
        .limit(1)
    )


def _insert_admission(
    session: Session,
    action: Action,
    *,
    attempt: ExecutionAttempt,
    state: SourcePacingState,
    spec: SourceAdmissionSpec,
    key: str,
) -> str | None:
    values = {
        "id": str(uuid4()),
        "admission_key": key,
        "tenant_id": action.tenant_id,
        "task_id": action.task_id,
        "lifecycle_epoch": spec.lifecycle_epoch,
        "source_pacing_state_id": state.id,
        "owner_type": spec.owner_type,
        "owner_id": spec.owner_id,
        "action_id": action.id,
        "attempt_id": attempt.id,
        "pacing_period_key": spec.period_key,
        "pacing_plan_hash": spec.plan_hash,
        "planned_release_at": spec.release_at,
        "call_not_before_at": spec.release_at,
        "source_gap_seconds": spec.source_gap_seconds,
    }
    dialect = session.get_bind().dialect.name
    insert = (
        pg_insert(SourcePacingAdmission)
        if dialect == "postgresql"
        else sqlite_insert(SourcePacingAdmission)
    )
    return session.scalar(
        insert.values(**values).on_conflict_do_nothing(
            index_elements=["admission_key"]
        ).returning(SourcePacingAdmission.id)
    )


def _lock_admission_by_key(session: Session, key: str) -> SourcePacingAdmission:
    admission = session.scalar(
        select(SourcePacingAdmission)
        .where(SourcePacingAdmission.admission_key == key)
        .with_for_update()
    )
    if admission is None:
        raise LookupError("pacing_source_admission_unavailable")
    return admission


def _bind_admission(
    admission: SourcePacingAdmission,
    *,
    action: Action,
    attempt: ExecutionAttempt,
) -> None:
    admission.action_id = action.id
    admission.attempt_id = attempt.id
    admission.state = "reserved"
    admission.version = int(admission.version or 1) + 1


def _admission_key(session: Session, action: Action) -> str:
    gateway_call_count = session.scalar(select(func.count(ExecutionAttempt.id)).where(
        ExecutionAttempt.action_id == action.id,
        ExecutionAttempt.gateway_call_started_at.is_not(None),
    )) or 0
    value = ":".join((
        action.id,
        str(int(action.assignment_revision or 1)),
        str(int(action.intent_revision or 1)),
        str(int(gateway_call_count)),
    ))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


__all__ = ["SourceAdmissionSpec", "lock_or_create_admission"]

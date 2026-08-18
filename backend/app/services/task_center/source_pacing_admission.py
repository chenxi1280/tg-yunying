from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import math
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.models import (
    Action,
    ChannelMessage,
    CommentFulfillmentObligation,
    ExecutionAttempt,
    ReactionFulfillmentObligation,
    SourcePacingAdmission,
    SourcePacingState,
    TaskDayLedger,
    TaskGroupDailyMessageSlot,
    ViewFulfillmentObligation,
)
from app.services._common import _now

from .pacing import PACING_CONTRACT_VERSION
from .source_owner_cursor import advance_owner_release
from .source_pacing_admission_settlement import (
    finished_before_gateway,
    settle_source_pacing_admission,
    unsettled_prior_admission,
)
from .source_pacing import wall_datetime


IDENTITY_RETRY_SECONDS = 60


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
    source_gap_seconds: int


def admit_source_paced_attempt(
    session: Session,
    action: Action,
    attempt: ExecutionAttempt,
    *,
    now_value: datetime | None = None,
) -> bool:
    if action.pacing_contract_version != PACING_CONTRACT_VERSION:
        return True
    if _reject_unsettled_prior_admission(session, action, attempt):
        return False
    timestamp = wall_datetime(now_value or _now())
    reservation = _reserve_source_admission(
        session,
        action=action,
        attempt=attempt,
        timestamp=timestamp,
    )
    if reservation is None:
        return False
    spec, state, admission, created = reservation
    if admission.state in {"call_started", "remote_unknown", "finished"}:
        _mark_prior_call_unknown(action, attempt, admission)
        return False
    not_before = _admission_not_before(
        action,
        state,
        admission=admission,
        spec=spec,
        created=created,
    )
    admission.attempt_id = attempt.id
    if timestamp < not_before:
        _defer_until(
            session,
            action=action,
            attempt=attempt,
            admission=admission,
            spec=spec,
            not_before=not_before,
        )
        return False
    _mark_call_started(
        state,
        admission,
        attempt,
        timestamp=timestamp,
        gap_seconds=spec.source_gap_seconds,
    )
    return True


def _reject_unsettled_prior_admission(
    session: Session,
    action: Action,
    attempt: ExecutionAttempt,
) -> bool:
    admission = unsettled_prior_admission(session, action)
    if admission is None:
        return False
    _mark_prior_call_unknown(action, attempt, admission)
    return True


def _reserve_source_admission(
    session: Session,
    *,
    action: Action,
    attempt: ExecutionAttempt,
    timestamp: datetime,
):
    try:
        spec = _source_admission_spec(session, action)
        state = _lock_or_create_source_state(session, action.tenant_id, spec)
        admission, created = _lock_or_create_admission(
            session,
            action,
            attempt=attempt,
            state=state,
            spec=spec,
        )
    except (LookupError, ValueError) as exc:
        _defer_identity_failure(
            action,
            attempt,
            reason=str(exc),
            timestamp=timestamp,
        )
        return None
    return spec, state, admission, created


def _admission_not_before(
    action: Action,
    state: SourcePacingState,
    *,
    admission: SourcePacingAdmission,
    spec: SourceAdmissionSpec,
    created: bool,
) -> datetime:
    not_before = (
        _call_not_before(action, state, spec)
        if created
        else wall_datetime(admission.call_not_before_at)
    )
    admission.call_not_before_at = not_before
    if created:
        state.next_call_not_before_at = not_before + timedelta(
            seconds=spec.source_gap_seconds
        )
    return not_before


def _source_admission_spec(session: Session, action: Action) -> SourceAdmissionSpec:
    owner, domain = _source_owner(session, action)
    release_at = getattr(owner, "release_not_before_at", None) or action.release_not_before_at
    plan_hash = str(getattr(owner, "pacing_plan_hash", None) or action.pacing_plan_hash or "")
    plan_total = int(getattr(owner, "pacing_plan_total", 0) or 0)
    if release_at is None or not plan_hash or plan_total <= 0:
        raise ValueError("pacing_source_identity_incomplete")
    period_start, deadline, period_key = _source_period(session, owner, domain)
    peer = _source_peer(action)
    if not peer or deadline <= period_start:
        raise ValueError("pacing_source_period_invalid")
    source_hash = _source_key_hash(peer)
    _freeze_owner_source_identity(
        action,
        owner,
        source_key_hash=source_hash,
        period_key=period_key,
    )
    return SourceAdmissionSpec(
        pacing_domain=domain,
        source_key_hash=source_hash,
        owner_type=owner.__tablename__,
        owner_id=str(owner.id),
        lifecycle_epoch=int(action.task_lifecycle_epoch or 1),
        period_key=period_key,
        plan_hash=plan_hash,
        release_at=wall_datetime(release_at),
        source_gap_seconds=max(
            1,
            math.ceil((deadline - period_start).total_seconds() / plan_total),
        ),
    )


def _source_owner(session: Session, action: Action):
    payload = action.payload if isinstance(action.payload, dict) else {}
    if action.task_type == "group_ai_chat" and action.action_type == "send_message":
        owner_id = action.primary_quantity_slot_id or action.obligation_id
        return _required_owner(session, TaskGroupDailyMessageSlot, owner_id), "ai_send"
    if action.action_type == "view_message":
        owner_id = payload.get("view_fulfillment_obligation_id") or action.obligation_id
        return _required_owner(session, ViewFulfillmentObligation, owner_id), "view"
    if action.action_type == "like_message":
        owner_id = payload.get("reaction_fulfillment_obligation_id") or action.obligation_id
        return _required_owner(session, ReactionFulfillmentObligation, owner_id), "reaction"
    if action.action_type == "post_comment":
        owner_id = payload.get("comment_fulfillment_obligation_id") or action.obligation_id
        return _required_owner(session, CommentFulfillmentObligation, owner_id), "comment"
    raise LookupError("pacing_source_owner_unsupported")


def _required_owner(session: Session, model, owner_id):
    owner = session.get(model, str(owner_id or ""))
    if owner is None:
        raise LookupError("pacing_source_owner_missing")
    return owner


def _source_period(session: Session, owner, domain: str) -> tuple[datetime, datetime, str]:
    if domain in {"ai_send", "view"}:
        ledger = session.get(TaskDayLedger, str(owner.task_day_ledger_id or ""))
        if ledger is None:
            raise LookupError("pacing_source_ledger_missing")
        return (
            wall_datetime(ledger.period_start_at),
            wall_datetime(ledger.deadline_at),
            str(ledger.id),
        )
    message = session.get(ChannelMessage, int(owner.channel_message_id or 0))
    if message is None:
        raise LookupError("pacing_source_message_missing")
    period_start = wall_datetime(message.published_at or message.created_at)
    return period_start, period_start + timedelta(days=1), f"message:{message.id}"


def _source_peer(action: Action) -> str:
    payload = action.payload if isinstance(action.payload, dict) else {}
    snapshot = payload.get("target_reference_snapshot")
    frozen_peer = snapshot.get("tg_peer_id") if isinstance(snapshot, dict) else ""
    return str(frozen_peer or payload.get("channel_id") or payload.get("chat_id") or "").strip()


def _source_key_hash(peer: str) -> str:
    normalized = peer.strip().lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _freeze_owner_source_identity(
    action: Action,
    owner,
    *,
    source_key_hash: str,
    period_key: str,
) -> None:
    values = (
        getattr(owner, "task_lifecycle_epoch", None),
        getattr(owner, "pacing_period_key", None),
        getattr(owner, "pacing_source_key_hash", None),
    )
    expected = (int(action.task_lifecycle_epoch or 1), period_key, source_key_hash)
    if any(value not in {None, expected[index]} for index, value in enumerate(values)):
        raise ValueError("pacing_source_identity_conflict")
    owner.task_lifecycle_epoch, owner.pacing_period_key, owner.pacing_source_key_hash = expected


def _lock_or_create_source_state(
    session: Session,
    tenant_id: int,
    spec: SourceAdmissionSpec,
) -> SourcePacingState:
    values = {
        "id": str(uuid4()),
        "tenant_id": tenant_id,
        "pacing_domain": spec.pacing_domain,
        "source_key_hash": spec.source_key_hash,
    }
    dialect = session.get_bind().dialect.name
    insert = pg_insert(SourcePacingState) if dialect == "postgresql" else sqlite_insert(SourcePacingState)
    session.execute(insert.values(**values).on_conflict_do_nothing(
        index_elements=["tenant_id", "pacing_domain", "source_key_hash"]
    ))
    state = session.scalar(
        select(SourcePacingState).where(
            SourcePacingState.tenant_id == tenant_id,
            SourcePacingState.pacing_domain == spec.pacing_domain,
            SourcePacingState.source_key_hash == spec.source_key_hash,
        ).with_for_update()
    )
    if state is None:
        raise LookupError("pacing_source_state_unavailable")
    return state


def _lock_or_create_admission(
    session: Session,
    action: Action,
    *,
    attempt: ExecutionAttempt,
    state: SourcePacingState,
    spec: SourceAdmissionSpec,
) -> tuple[SourcePacingAdmission, bool]:
    key = _admission_key(session, action)
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
    insert = pg_insert(SourcePacingAdmission) if dialect == "postgresql" else sqlite_insert(SourcePacingAdmission)
    inserted_id = session.scalar(
        insert.values(**values).on_conflict_do_nothing(
            index_elements=["admission_key"]
        ).returning(SourcePacingAdmission.id)
    )
    admission = session.scalar(
        select(SourcePacingAdmission)
        .where(SourcePacingAdmission.admission_key == key)
        .with_for_update()
    )
    if admission is None:
        raise LookupError("pacing_source_admission_unavailable")
    if admission.source_pacing_state_id != state.id:
        raise ValueError("pacing_source_admission_conflict")
    recycled = finished_before_gateway(session, admission)
    if recycled:
        admission.state = "reserved"
        admission.attempt_id = attempt.id
    return admission, inserted_id is not None or recycled


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


def _mark_call_started(
    state: SourcePacingState,
    admission: SourcePacingAdmission,
    attempt: ExecutionAttempt,
    *,
    timestamp: datetime,
    gap_seconds: int,
) -> None:
    state.last_call_started_at = timestamp
    state.last_source_gap_seconds = gap_seconds
    next_after_call = timestamp + timedelta(seconds=gap_seconds)
    existing_next = (
        wall_datetime(state.next_call_not_before_at)
        if state.next_call_not_before_at is not None
        else next_after_call
    )
    state.next_call_not_before_at = max(existing_next, next_after_call)
    state.revision = int(state.revision or 0) + 1
    state.version = int(state.version or 1) + 1
    admission.state = "call_started"
    admission.attempt_id = attempt.id
    admission.version = int(admission.version or 1) + 1


def _defer_until(
    session: Session,
    *,
    action: Action,
    attempt: ExecutionAttempt,
    admission: SourcePacingAdmission,
    spec: SourceAdmissionSpec,
    not_before: datetime,
) -> None:
    admission.state = "reserved"
    admission.version = int(admission.version or 1) + 1
    advance_owner_release(
        session,
        owner_type=spec.owner_type,
        owner_id=spec.owner_id,
        not_before=not_before,
    )
    _defer_action(
        action,
        attempt,
        code="pacing_source_not_before",
        not_before=not_before,
    )


def _defer_identity_failure(
    action: Action,
    attempt: ExecutionAttempt,
    *,
    reason: str,
    timestamp: datetime,
) -> None:
    _defer_action(
        action,
        attempt,
        code=reason or "pacing_source_admission_unavailable",
        not_before=timestamp + timedelta(seconds=IDENTITY_RETRY_SECONDS),
    )


def _defer_action(
    action: Action,
    attempt: ExecutionAttempt,
    *,
    code: str,
    not_before: datetime,
) -> None:
    action.status = "pending"
    action.scheduled_at = not_before
    action.release_not_before_at = max(
        wall_datetime(action.release_not_before_at),
        not_before,
    ) if action.release_not_before_at is not None else not_before
    action.executed_at = None
    action.lease_owner = ""
    action.lease_expires_at = None
    action.claim_owner = ""
    action.claim_token = ""
    action.claim_expires_at = None
    action.result = {
        **(action.result or {}),
        "error_code": code,
        "validation_stage": "source_pacing_admission",
        "call_not_before_at": not_before.isoformat(),
    }
    attempt.status = "skipped_before_gateway"
    attempt.after_call_at = _now()
    attempt.failure_type = code
    attempt.failure_detail = "source pacing admission deferred before Gateway"
    attempt.result_snapshot = dict(action.result)


def _mark_prior_call_unknown(
    action: Action,
    attempt: ExecutionAttempt,
    admission: SourcePacingAdmission,
) -> None:
    action.status = "unknown_after_send"
    action.executed_at = _now()
    action.lease_owner = ""
    action.lease_expires_at = None
    action.result = {
        **(action.result or {}),
        "error_code": "pacing_source_prior_call_started",
        "validation_stage": "source_pacing_admission",
        "source_pacing_admission_id": admission.id,
    }
    attempt.status = "skipped_before_gateway"
    attempt.after_call_at = _now()
    attempt.failure_type = "pacing_source_prior_call_started"
    attempt.result_snapshot = dict(action.result)


__all__ = [
    "SourceAdmissionSpec",
    "admit_source_paced_attempt",
    "settle_source_pacing_admission",
]

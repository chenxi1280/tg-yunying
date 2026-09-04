from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.models import Action, GenerationJob, Task
from app.services._common import _now

from .ai_generation_claim_lifecycle import owns_generation_claim
from .ai_generation_job_finish import finish_owned_job
from .ai_dialogue_chain import resolve_waiting_dialogue_dependencies
from .ai_generation_timing import GENERATION_LEASE, GENERATION_LOOKAHEAD, generation_not_before, generation_send_time_expression
from .datetime_compat import is_after_or_equal
from .fulfillment_activation import CURRENT_CONTRACT_VERSION
from .fulfillment_remote_facts import ensure_action_obligation
from .generation_wait import GenerationWaitSpec, defer_generation_wait, latest_safe_send_at
from .ai_quality_stats import record_provider_admission_unavailable
from .provider_admission import (
    ProviderAdmissionBlocked,
    ProviderAdmissionUnavailable,
    ensure_claim_admission,
)


GENERATABLE_STATUSES = ("pending", "ai_result_persist_unknown")
OPEN_GENERATION_JOB_PREDICATE = "state IN ('pending','generating','unknown')"


class _ClaimConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class ParallelGenerationClaim:
    action_id: str
    job_id: str
    owner: str
    token: str
    job_version: int
    generation_lease_epoch: int


def claim_parallel_generation(
    session_factory,
    *,
    owner: str,
    limit: int,
) -> tuple[ParallelGenerationClaim, ...]:
    with session_factory() as session:
        resolve_waiting_dialogue_dependencies(session, limit=max(1, limit * 3))
        candidates = list(session.scalars(_candidate_statement(limit)))
        if not candidates:
            session.commit()
            return ()
        if not _batch_claim_admitted(session, candidates[0]):
            session.commit()
            return ()
        claims: list[ParallelGenerationClaim] = []
        for action in candidates:
            if len(claims) >= limit:
                break
            try:
                with session.begin_nested():
                    claim = _claim_one(session, action, owner)
                    if claim is not None:
                        claims.append(claim)
            except _ClaimConflict:
                continue
        session.commit()
        return tuple(claims)


def _batch_claim_admitted(session: Session, action: Action) -> bool:
    try:
        ensure_claim_admission(session)
    except ProviderAdmissionBlocked:
        return False
    except ProviderAdmissionUnavailable:
        record_provider_admission_unavailable(session, action)
        session.commit()
        raise
    return True


def finish_generation_job(
    session_factory,
    claim: ParallelGenerationClaim,
    *,
    state: str,
    generation_stage: str | None = None,
) -> None:
    with session_factory() as session:
        job = session.get(GenerationJob, claim.job_id)
        action = session.get(Action, claim.action_id)
        finish_owned_job(
            session,
            claim,
            job=job,
            action=action,
            state=state,
            generation_stage=generation_stage,
        )
        session.commit()


def settle_deferred_parallel_claim(
    session_factory,
    claim: ParallelGenerationClaim,
) -> None:
    with session_factory() as session:
        job = session.get(GenerationJob, claim.job_id)
        action = session.get(Action, claim.action_id)
        if job is None or action is None:
            raise RuntimeError("parallel_generation_deferred_owner_missing")
        if job.state == "pending" and not job.generation_owner_id:
            if action.status != "pending":
                raise RuntimeError("parallel_generation_deferred_action_invalid")
            return
        if owns_generation_claim(action, claim.owner, claim.token):
            from .ai_generation_claim_lifecycle import release_generation_claim

            release_generation_claim(action, dict(action.payload or {}))
        elif action.status != "pending":
            raise RuntimeError("parallel_generation_action_claim_lost")
        finish_owned_job(session, claim, job=job, action=action, state="pending")
        session.commit()


def defer_parallel_generation(
    session_factory,
    claim: ParallelGenerationClaim,
    *,
    next_retry_at: datetime,
) -> None:
    with session_factory() as session:
        job = session.get(GenerationJob, claim.job_id)
        action = session.get(Action, claim.action_id)
        if job is None or job.generation_owner_id != claim.owner:
            raise RuntimeError("parallel_generation_job_claim_lost")
        if not owns_generation_claim(action, claim.owner, claim.token):
            raise RuntimeError("parallel_generation_action_claim_lost")
        task = session.get(Task, action.task_id)
        if task is None:
            raise RuntimeError("parallel_generation_task_missing")
        defer_generation_wait(
            session,
            task,
            action,
            job,
            GenerationWaitSpec(
                stage="waiting_provider",
                error_code="provider_route_deferred",
                error_detail="provider route temporarily unavailable",
                shortfall_kind="provider_capacity",
                evaluator_evidence={},
                next_retry_at=next_retry_at,
            ),
        )
        session.commit()


def _candidate_statement(limit: int):
    now_value = _now()
    payload_status = Action.payload["ai_generation_status"].as_string()
    message_text = Action.payload["message_text"].as_string()
    deferred_job = select(GenerationJob.id).where(
        GenerationJob.obligation_type == Action.obligation_type,
        GenerationJob.obligation_id == Action.obligation_id,
        GenerationJob.state.in_(("pending", "generating", "unknown")),
        or_(
            GenerationJob.generation_not_before_at > now_value,
            GenerationJob.next_retry_at > now_value,
        ),
    ).exists()
    return (
        select(Action)
        .join(Task, Task.id == Action.task_id)
        .where(
            Action.task_type == "group_ai_chat",
            Action.action_type == "send_message",
            Action.status == "pending",
            Action.account_id.is_not(None),
            generation_send_time_expression() <= now_value + GENERATION_LOOKAHEAD,
            Task.status == "running",
            Task.deleted_at.is_(None),
            Task.fulfillment_contract_version == CURRENT_CONTRACT_VERSION,
            Action.task_lifecycle_epoch == Task.task_lifecycle_epoch,
            payload_status.in_(GENERATABLE_STATUSES),
            func.coalesce(message_text, "") == "",
            ~deferred_job,
        )
        .order_by(Action.scheduled_at, Action.task_id, Action.id)
        .limit(max(1, limit * 3))
    )


def _claim_one(
    session: Session,
    action: Action,
    owner: str,
) -> ParallelGenerationClaim | None:
    if not ensure_action_obligation(session, action):
        return None
    job = _generation_job(session, action)
    now_value = _now()
    if not _job_available(job, now_value):
        raise _ClaimConflict(action.id)
    token = str(uuid4())
    expected_action_version = int(action.action_version or 1)
    expected_job_version = int(job.job_version or 1)
    changed_job = _claim_job(
        session,
        job,
        owner=owner,
        now_value=now_value,
        expected_version=expected_job_version,
    )
    changed_action = _claim_action(
        session,
        action,
        owner=owner,
        token=token,
        now_value=now_value,
        expected_version=expected_action_version,
        job_id=job.id,
    )
    if changed_job != 1 or changed_action != 1:
        raise _ClaimConflict(action.id)
    return ParallelGenerationClaim(
        action.id,
        job.id,
        owner,
        token,
        expected_job_version + 1,
        int(job.generation_lease_epoch or 0) + 1,
    )


def _generation_job(session: Session, action: Action) -> GenerationJob:
    values = {
        "tenant_id": action.tenant_id,
        "task_id": action.task_id,
        "task_lifecycle_epoch": int(action.task_lifecycle_epoch or 1),
        "obligation_type": action.obligation_type,
        "obligation_id": action.obligation_id,
        "generation_sequence": int(action.materialization_version or 1),
        "context_snapshot_version": _context_version(action),
        "generation_not_before_at": generation_not_before(action),
        "latest_safe_send_at": latest_safe_send_at(session, action),
        "context_snapshot_hash": _context_hash(action),
        "assignment_revision": int(action.assignment_revision or 1),
        "intent_revision": int(action.intent_revision or 1),
        "candidate_hash": str(action.candidate_hash or ""),
        "evaluator_evidence": dict(
            (action.result or {}).get("evaluator_evidence") or {},
        ),
    }
    table = GenerationJob.__table__
    statement = pg_insert(table) if session.get_bind().dialect.name == "postgresql" else sqlite_insert(table)
    session.execute(statement.values(**values).on_conflict_do_nothing(
        index_elements=["obligation_type", "obligation_id"],
        index_where=text(OPEN_GENERATION_JOB_PREDICATE),
    ))
    job = session.scalar(select(GenerationJob).where(
        GenerationJob.obligation_type == action.obligation_type,
        GenerationJob.obligation_id == action.obligation_id,
        GenerationJob.state.in_(("pending", "generating", "unknown")),
    ))
    if job is None:
        raise RuntimeError("parallel_generation_job_missing")
    return job


def _claim_job(session, job, *, owner, now_value, expected_version) -> int:
    return session.execute(
        update(GenerationJob)
        .where(
            GenerationJob.id == job.id,
            GenerationJob.job_version == expected_version,
            or_(
                GenerationJob.state == "pending",
                and_(
                    GenerationJob.state == "generating",
                    GenerationJob.lease_expires_at <= now_value,
                ),
            ),
        )
        .values(
            state="generating",
            generation_owner_id=owner,
            generation_lease_epoch=int(job.generation_lease_epoch or 0) + 1,
            lease_expires_at=now_value + GENERATION_LEASE,
            job_version=expected_version + 1,
        )
        .execution_options(synchronize_session=False)
    ).rowcount


def _claim_action(
    session,
    action,
    *,
    owner,
    token,
    now_value,
    expected_version,
    job_id,
) -> int:
    payload = dict(action.payload or {})
    payload.update({
        "ai_generation_status": "generating",
        "ai_generation_claim_owner": owner,
        "ai_generation_claim_token": token,
        "generation_job_id": job_id,
    })
    return session.execute(
        update(Action)
        .where(
            Action.id == action.id,
            Action.status == "pending",
            Action.action_version == expected_version,
        )
        .values(
            status="executing",
            payload=payload,
            claim_owner=owner,
            claim_token=token,
            lease_owner=owner,
            lease_expires_at=now_value + GENERATION_LEASE,
            action_version=expected_version + 1,
        )
    ).rowcount


def _job_available(job: GenerationJob, now_value: datetime) -> bool:
    not_before_ready = (
        job.generation_not_before_at is None
        or is_after_or_equal(now_value, job.generation_not_before_at)
    )
    return bool(
        (
            job.state == "pending"
            and not_before_ready
            and (
                job.next_retry_at is None
                or is_after_or_equal(now_value, job.next_retry_at)
            )
        )
        or (
            job.state == "generating"
            and job.lease_expires_at is not None
            and is_after_or_equal(now_value, job.lease_expires_at)
        )
    )


def _context_version(action: Action) -> int:
    payload = dict(action.payload or {})
    return int(payload.get("context_snapshot_version") or 1)


def _context_hash(action: Action) -> str:
    payload = dict(action.payload or {})
    snapshot = {
        "context_message_ids": payload.get("context_message_ids") or [],
        "reply_to_message_id": payload.get("reply_to_message_id"),
        "context_snapshot_version": _context_version(action),
    }
    encoded = json.dumps(
        snapshot,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "ParallelGenerationClaim",
    "claim_parallel_generation",
    "defer_parallel_generation",
    "finish_generation_job",
    "settle_deferred_parallel_claim",
]

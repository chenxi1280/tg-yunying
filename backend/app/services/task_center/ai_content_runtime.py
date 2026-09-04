from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    AiContentWindowPlan,
    AiContentWindowPlanSlot,
    ContextScopeRevision,
    FulfillmentShortfallFact,
    GenerationJob,
)
from app.services._common import _now

from .datetime_compat import is_after_or_equal


CURRENT_SLOT_STATES = ("frozen", "claimed", "candidate_ready", "gateway_bound")
PRE_GATEWAY_SLOT_STATES = ("claimed", "candidate_ready")
SHORTFALL_KINDS = frozenset({"quality", "provider_capacity", "context_stale", "pacing_capacity"})
TERMINAL_JOB_STATES = ("failed", "cancelled")


class AiContentRuntimeConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class WindowScope:
    tenant_id: int
    task_id: str
    task_lifecycle_epoch: int
    scope_type: str
    scope_id: str
    pacing_plan_hash: str
    period_key: str
    window_start_at: datetime
    window_end_at: datetime
    task_config_revision: int
    content_policy_hash: str


@dataclass(frozen=True)
class WindowSlotSpec:
    slot_ordinal: int
    obligation_type: str
    obligation_id: str
    generation_sequence: int
    account_id: int
    due_at: datetime
    context_scope_revision: int
    context_snapshot_hash: str
    context_route: str
    content_mode: str
    route_evidence_hash: str
    prompt_contract_version: str


@dataclass(frozen=True)
class ShortfallSpec:
    tenant_id: int
    task_id: str
    task_lifecycle_epoch: int
    owner_type: str
    owner_id: str
    period_key: str
    kind: str
    reason_code: str
    requested_quantity: int
    settled_quantity: int
    evidence_hash: str


def bump_context_revision(
    session: Session,
    *,
    tenant_id: int,
    scope_type: str,
    scope_id: str,
    snapshot_hash: str,
    human_message_id: str | None = None,
    reply_target_hash: str | None = None,
) -> ContextScopeRevision:
    table = ContextScopeRevision.__table__
    statement = _insert(session, table).values(
        tenant_id=tenant_id,
        scope_type=scope_type,
        scope_id=scope_id,
        context_scope_revision=1,
        context_snapshot_hash=snapshot_hash,
        last_human_message_id=human_message_id or "",
        reply_target_hash=reply_target_hash or "",
        updated_at=_now(),
    )
    update_values = {
        "context_scope_revision": table.c.context_scope_revision + 1,
        "context_snapshot_hash": snapshot_hash,
        "updated_at": _now(),
    }
    if human_message_id is not None:
        update_values["last_human_message_id"] = human_message_id
    if reply_target_hash is not None:
        update_values["reply_target_hash"] = reply_target_hash
    statement = statement.on_conflict_do_update(
        index_elements=["tenant_id", "scope_type", "scope_id"],
        set_=update_values,
    ).returning(*table.c)
    row = session.execute(statement).mappings().one_or_none()
    if row is None:
        raise AiContentRuntimeConflict("context_scope_revision_upsert_failed")
    return ContextScopeRevision(**dict(row))


def freeze_window_plan(
    session: Session,
    scope: WindowScope,
    slots: tuple[WindowSlotSpec, ...],
) -> AiContentWindowPlan:
    if not slots:
        raise ValueError("ai_content_window_slots_empty")
    plan_hash = _hash({"scope": asdict(scope), "slots": [asdict(item) for item in slots]})
    existing = _window_plan(session, scope)
    if existing is not None:
        if existing.plan_hash != plan_hash:
            raise AiContentRuntimeConflict("ai_content_window_scope_conflict")
        return existing
    try:
        with session.begin_nested():
            plan = AiContentWindowPlan(**asdict(scope), state="frozen", plan_hash=plan_hash)
            session.add(plan)
            session.flush()
            session.add_all(
                AiContentWindowPlanSlot(plan_id=plan.id, **asdict(slot))
                for slot in slots
            )
            session.flush()
        return plan
    except IntegrityError as exc:
        winner = _window_plan(session, scope)
        if winner is None or winner.plan_hash != plan_hash:
            raise AiContentRuntimeConflict("ai_content_window_concurrent_conflict") from exc
        return winner


def claim_window_slot(
    session: Session,
    job: GenerationJob,
    *,
    lease_duration: timedelta,
) -> AiContentWindowPlanSlot:
    slot = _current_slot(session, job.obligation_type, job.obligation_id)
    if slot is None:
        raise AiContentRuntimeConflict("ai_content_window_slot_missing")
    now_value = _now()
    claimable = slot.state == "frozen" or (
        slot.state == "claimed"
        and slot.lease_expires_at is not None
        and is_after_or_equal(now_value, slot.lease_expires_at)
    )
    if not claimable:
        raise AiContentRuntimeConflict("ai_content_window_slot_not_claimable")
    changed = session.execute(update(AiContentWindowPlanSlot).where(
        AiContentWindowPlanSlot.id == slot.id,
        AiContentWindowPlanSlot.version == slot.version,
    ).values(
        state="claimed",
        claimed_by_job_id=job.id,
        lease_epoch=slot.lease_epoch + 1,
        lease_expires_at=now_value + lease_duration,
        version=slot.version + 1,
    )).rowcount
    if changed != 1:
        raise AiContentRuntimeConflict("ai_content_window_slot_claim_conflict")
    session.refresh(slot)
    _bind_job_to_slot(session, job, slot)
    return slot


def mark_candidate_ready(
    session: Session,
    job: GenerationJob,
    *,
    candidate_hash: str,
) -> None:
    slot = _claimed_slot(session, job)
    if slot.state == "candidate_ready" and job.candidate_hash == candidate_hash:
        return
    changed = session.execute(update(AiContentWindowPlanSlot).where(
        AiContentWindowPlanSlot.id == slot.id,
        AiContentWindowPlanSlot.version == slot.version,
        AiContentWindowPlanSlot.state == "claimed",
    ).values(
        state="candidate_ready",
        lease_expires_at=None,
        version=slot.version + 1,
    )).rowcount
    if changed != 1:
        raise AiContentRuntimeConflict("ai_content_window_candidate_conflict")
    session.refresh(slot)
    job.candidate_hash = candidate_hash
    job.generation_stage = "reviewing"
    job.stage_version += 1


def bind_candidate_to_gateway(
    session: Session,
    job: GenerationJob,
    *,
    candidate_hash: str,
    task_config_revision: int,
) -> None:
    slot = _claimed_slot(session, job)
    if slot.state == "gateway_bound" and job.candidate_hash == candidate_hash:
        return
    if slot.state != "candidate_ready" or job.candidate_hash != candidate_hash:
        raise AiContentRuntimeConflict("generation_candidate_binding_invalid")
    plan = session.get(AiContentWindowPlan, slot.plan_id)
    if plan is None:
        raise AiContentRuntimeConflict("ai_content_window_plan_missing")
    stale_reason = _gateway_stale_reason(
        session, plan, job, task_config_revision=task_config_revision,
    )
    if stale_reason:
        _mark_gateway_candidate_stale(slot, job, stale_reason)
        raise AiContentRuntimeConflict(stale_reason)
    slot.state = "gateway_bound"
    slot.lease_expires_at = None
    slot.version += 1
    job.generation_stage = "gateway_bound"
    job.stage_version += 1


def _gateway_stale_reason(
    session: Session,
    plan: AiContentWindowPlan,
    job: GenerationJob,
    *,
    task_config_revision: int,
) -> str:
    revision = session.scalar(select(ContextScopeRevision).where(
        ContextScopeRevision.tenant_id == plan.tenant_id,
        ContextScopeRevision.scope_type == plan.scope_type,
        ContextScopeRevision.scope_id == plan.scope_id,
    ))
    if revision and revision.context_scope_revision > job.context_snapshot_version:
        return "context_stale"
    contract = dict(job.evaluator_evidence or {}).get("generation_contract") or {}
    frozen_topic_revision = int(dict(contract).get("task_topic_revision") or 0)
    if frozen_topic_revision and frozen_topic_revision != task_config_revision:
        return "policy_stale"
    return ""


def _mark_gateway_candidate_stale(
    slot: AiContentWindowPlanSlot,
    job: GenerationJob,
    reason: str,
) -> None:
    slot.state = "invalidated"
    slot.lease_expires_at = None
    slot.version += 1
    job.state = "failed"
    job.generation_stage = reason
    job.evaluator_evidence = {
        **dict(job.evaluator_evidence or {}),
        "invalidation_reason": reason,
    }
    job.job_version += 1


def invalidate_pre_gateway_slot(
    session: Session,
    job: GenerationJob,
    *,
    reason: str,
) -> None:
    slot = _claimed_slot(session, job)
    if slot.state not in PRE_GATEWAY_SLOT_STATES:
        raise AiContentRuntimeConflict("ai_content_window_slot_not_invalidatable")
    invalidate_pre_gateway_window_slot(slot)
    job.state = "failed"
    job.generation_stage = "quality_wait"
    job.evaluator_evidence = {**dict(job.evaluator_evidence or {}), "invalidation_reason": reason}
    job.job_version += 1


def invalidate_job_pre_gateway_slot(
    session: Session,
    job: GenerationJob,
) -> bool:
    if not job.window_slot_id:
        return False
    slot = session.get(AiContentWindowPlanSlot, job.window_slot_id)
    if slot is None:
        return False
    return invalidate_pre_gateway_window_slot(slot)


def invalidate_pre_gateway_window_slot(
    slot: AiContentWindowPlanSlot,
) -> bool:
    if slot.state == "gateway_bound":
        raise AiContentRuntimeConflict("ai_content_window_gateway_bound")
    if slot.state not in PRE_GATEWAY_SLOT_STATES:
        return False
    slot.state = "invalidated"
    slot.claimed_by_job_id = None
    slot.lease_expires_at = None
    slot.version = int(slot.version or 1) + 1
    return True


def invalidate_terminal_pre_gateway_obligation_slot(
    session: Session,
    *,
    obligation_type: str,
    obligation_id: str,
) -> bool:
    slot = session.scalar(
        _terminal_pre_gateway_slot_query()
        .where(
            AiContentWindowPlanSlot.obligation_type == obligation_type,
            AiContentWindowPlanSlot.obligation_id == obligation_id,
        )
        .with_for_update()
    )
    return bool(slot and invalidate_pre_gateway_window_slot(slot))


def recover_terminal_pre_gateway_window_slots(
    session: Session,
    limit: int,
    *,
    task_type: str | None = None,
) -> int:
    from .generation_recovery_scope import generation_task_filter
    slots = session.scalars(
        _terminal_pre_gateway_slot_query()
        .where(generation_task_filter(task_type))
        .order_by(AiContentWindowPlanSlot.created_at, AiContentWindowPlanSlot.id)
        .limit(max(1, int(limit)))
        .with_for_update(skip_locked=True)
    )
    return sum(invalidate_pre_gateway_window_slot(slot) for slot in slots)


def _terminal_pre_gateway_slot_query():
    return (
        select(AiContentWindowPlanSlot)
        .join(
            GenerationJob,
            GenerationJob.id == AiContentWindowPlanSlot.claimed_by_job_id,
        )
        .where(
            AiContentWindowPlanSlot.state.in_(PRE_GATEWAY_SLOT_STATES),
            GenerationJob.state.in_(TERMINAL_JOB_STATES),
        )
    )


def settle_shortfall(
    session: Session,
    spec: ShortfallSpec,
) -> FulfillmentShortfallFact:
    if spec.kind not in SHORTFALL_KINDS:
        raise ValueError(f"fulfillment_shortfall_kind_invalid:{spec.kind}")
    if spec.settled_quantity > spec.requested_quantity:
        raise ValueError("fulfillment_shortfall_quantity_invalid")
    table = FulfillmentShortfallFact.__table__
    session.execute(_insert(session, table).values(**asdict(spec)).on_conflict_do_nothing(
        index_elements=["owner_type", "owner_id", "period_key"],
    ))
    fact = session.scalar(select(FulfillmentShortfallFact).where(
        FulfillmentShortfallFact.owner_type == spec.owner_type,
        FulfillmentShortfallFact.owner_id == spec.owner_id,
        FulfillmentShortfallFact.period_key == spec.period_key,
    ))
    if fact is None or fact.evidence_hash != spec.evidence_hash:
        raise AiContentRuntimeConflict("fulfillment_shortfall_identity_conflict")
    return fact


def defer_generation_job(
    job: GenerationJob,
    *,
    stage: str,
    next_retry_at: datetime,
) -> None:
    if job.latest_safe_send_at is not None and is_after_or_equal(
        next_retry_at,
        job.latest_safe_send_at,
    ):
        raise AiContentRuntimeConflict("generation_retry_after_deadline")
    job.state = "pending"
    job.generation_stage = stage
    job.next_retry_at = next_retry_at
    job.generation_owner_id = ""
    job.lease_expires_at = None
    job.stage_version += 1
    job.job_version += 1


def context_message_hash(*, remote_message_id: str, content: str, reply_hash: str = "") -> str:
    return _hash({
        "remote_message_id": remote_message_id,
        "content": content,
        "reply_hash": reply_hash,
    })


def _window_plan(session: Session, scope: WindowScope) -> AiContentWindowPlan | None:
    return session.scalar(select(AiContentWindowPlan).where(
        AiContentWindowPlan.tenant_id == scope.tenant_id,
        AiContentWindowPlan.task_id == scope.task_id,
        AiContentWindowPlan.task_lifecycle_epoch == scope.task_lifecycle_epoch,
        AiContentWindowPlan.scope_type == scope.scope_type,
        AiContentWindowPlan.scope_id == scope.scope_id,
        AiContentWindowPlan.pacing_plan_hash == scope.pacing_plan_hash,
        AiContentWindowPlan.period_key == scope.period_key,
        AiContentWindowPlan.window_start_at == scope.window_start_at,
        AiContentWindowPlan.window_end_at == scope.window_end_at,
        AiContentWindowPlan.task_config_revision == scope.task_config_revision,
        AiContentWindowPlan.content_policy_hash == scope.content_policy_hash,
    ))


def _current_slot(session: Session, obligation_type: str, obligation_id: str):
    return session.scalar(select(AiContentWindowPlanSlot).where(
        AiContentWindowPlanSlot.obligation_type == obligation_type,
        AiContentWindowPlanSlot.obligation_id == obligation_id,
        AiContentWindowPlanSlot.state.in_(CURRENT_SLOT_STATES),
    ))


def _claimed_slot(session: Session, job: GenerationJob) -> AiContentWindowPlanSlot:
    slot = session.scalar(select(AiContentWindowPlanSlot).where(
        AiContentWindowPlanSlot.claimed_by_job_id == job.id,
        AiContentWindowPlanSlot.state.in_(("claimed", "candidate_ready", "gateway_bound")),
    ))
    if slot is None:
        raise AiContentRuntimeConflict("ai_content_window_job_binding_missing")
    return slot


def _bind_job_to_slot(
    session: Session,
    job: GenerationJob,
    slot: AiContentWindowPlanSlot,
) -> None:
    plan = session.get(AiContentWindowPlan, slot.plan_id)
    if plan is None:
        raise AiContentRuntimeConflict("ai_content_window_plan_missing")
    job.window_slot_id = slot.id
    job.window_plan_hash = plan.plan_hash
    job.context_snapshot_version = slot.context_scope_revision
    job.context_snapshot_hash = slot.context_snapshot_hash
    job.context_route = slot.context_route
    job.content_mode = slot.content_mode
    job.route_evidence_hash = slot.route_evidence_hash
    job.prompt_contract_version = slot.prompt_contract_version
    job.generation_stage = "planning"
    job.stage_version += 1


def _hash(value: dict) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _insert(session: Session, table):  # noqa: ANN001
    return pg_insert(table) if session.get_bind().dialect.name == "postgresql" else sqlite_insert(table)


__all__ = [
    "AiContentRuntimeConflict",
    "ShortfallSpec",
    "WindowScope",
    "WindowSlotSpec",
    "bump_context_revision",
    "bind_candidate_to_gateway",
    "claim_window_slot",
    "context_message_hash",
    "defer_generation_job",
    "freeze_window_plan",
    "invalidate_job_pre_gateway_slot",
    "invalidate_pre_gateway_slot",
    "invalidate_pre_gateway_window_slot",
    "invalidate_terminal_pre_gateway_obligation_slot",
    "mark_candidate_ready",
    "recover_terminal_pre_gateway_window_slots",
    "settle_shortfall",
]

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AiContentWindowPlanSlot, ContextScopeRevision, GenerationJob


class ContextRevisionBindingConflict(RuntimeError):
    pass


def synchronize_generation_context(
    session: Session,
    job: GenerationJob,
    *,
    tenant_id: int,
    scope_type: str,
    scope_id: str,
) -> None:
    revision = session.scalar(select(ContextScopeRevision).where(
        ContextScopeRevision.tenant_id == tenant_id,
        ContextScopeRevision.scope_type == scope_type,
        ContextScopeRevision.scope_id == scope_id,
    ))
    if revision is None or revision.context_scope_revision <= job.context_snapshot_version:
        return
    _invalidate_old_slot(session, job)
    job.context_snapshot_version = revision.context_scope_revision
    job.context_snapshot_hash = revision.context_snapshot_hash
    job.candidate_hash = ""
    job.window_slot_id = None
    job.window_plan_hash = ""
    job.generation_stage = "planning"
    job.stage_version += 1
    job.job_version += 1


def _invalidate_old_slot(session: Session, job: GenerationJob) -> None:
    if not job.window_slot_id:
        return
    slot = session.get(AiContentWindowPlanSlot, job.window_slot_id)
    if slot is None or slot.claimed_by_job_id != job.id:
        raise ContextRevisionBindingConflict("generation_window_binding_invalid")
    if slot.state not in {"claimed", "candidate_ready"}:
        raise ContextRevisionBindingConflict("context_changed_after_gateway_binding")
    slot.state = "invalidated"
    slot.lease_expires_at = None
    slot.version += 1


__all__ = ["ContextRevisionBindingConflict", "synchronize_generation_context"]

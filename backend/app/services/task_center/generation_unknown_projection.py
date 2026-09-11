"""Recognize an authoritative unknown job before repairing its stale projection."""
from sqlalchemy import select

from app.models import (Action, AiContentWindowPlanSlot, ExecutionAttempt, GenerationJob,
                        FulfillmentRemoteFact, GatewayRequestEvidenceJournal, SourcePacingAdmission)
from .generation_action_identity import current_generation_action, job_matches_action


def owns_settled_unknown_projection(session, job: GenerationJob, action: Action) -> bool:
    if job.state != "unknown":
        return False
    session.refresh(job, with_for_update=True)
    if not _settled_provider_unknown(job):
        return False
    if not job_matches_action(job, action):
        return False
    current = current_generation_action(session, job)
    if current is None or current.id != action.id:
        return False
    if action.status == "unknown_after_send":
        return False
    if job.window_slot_id and session.scalar(select(AiContentWindowPlanSlot.state).where(
            AiContentWindowPlanSlot.id == job.window_slot_id)) == "gateway_bound":
        return False
    return not _has_remote_evidence(session, action)


def _settled_provider_unknown(job):
    return (job.state == "unknown" and not job.generation_owner_id and not job.lease_expires_at
            and job.generation_stage != "gateway_reconcile_required")


def _has_remote_evidence(session, action):
    called = session.scalar(select(ExecutionAttempt.id).where(
        ExecutionAttempt.action_id == action.id,
        ExecutionAttempt.gateway_call_started_at.is_not(None),
    ).limit(1)) is not None
    journal = session.scalar(select(GatewayRequestEvidenceJournal.id).where(
        GatewayRequestEvidenceJournal.action_id == action.id).limit(1)) is not None
    fact = session.scalar(select(FulfillmentRemoteFact.fact_id).where(
        FulfillmentRemoteFact.action_id == action.id).limit(1)) is not None
    source = session.scalar(select(SourcePacingAdmission.id).where(
        SourcePacingAdmission.action_id == action.id,
        SourcePacingAdmission.state.in_(("call_started", "remote_unknown", "finished")),
    ).limit(1)) is not None
    return called or journal or fact or source

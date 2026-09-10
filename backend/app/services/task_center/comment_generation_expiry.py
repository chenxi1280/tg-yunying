"""Settle a missed preparation deadline through the existing shortfall contract."""
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Action, CommentFulfillmentObligation, FulfillmentObligationProjection, GenerationJob, Task

from .channel_remote_evidence import action_remote_mutation_evidence
from .generation_provider_lineage import unresolved_generation_lineages
from .generation_wait import GenerationWaitSpec, defer_generation_wait
from .safe_settlement_resources import _settle_action_pacing_reservation
from .source_pacing_release import release_source_pacing_admissions_before_gateway


EXPIRY_CODE = "generation_timing_preparation_deadline_missed"


def settle_expired_preparation(
    session: Session,
    action: Action,
    *,
    task: Task,
    job: GenerationJob,
) -> None:
    if (job.tenant_id, job.task_id, job.task_lifecycle_epoch) != (
        action.tenant_id, action.task_id, action.task_lifecycle_epoch,
    ) or job.generation_owner_id != action.claim_owner:
        raise RuntimeError("comment_preparation_expiry_owner_mismatch")
    if unresolved_generation_lineages(session, (job,)):
        raise RuntimeError("comment_preparation_expiry_provider_unresolved")
    if action_remote_mutation_evidence(session, action).state not in {None, "false"}:
        raise RuntimeError("comment_preparation_expiry_remote_evidence_unsafe")
    if job.latest_safe_send_at is None:
        raise ValueError("comment_preparation_expiry_deadline_missing")
    _lock_obligation_owners(session, action)
    outcome = defer_generation_wait(session, task, action, job, GenerationWaitSpec(
        stage="preparation", error_code=EXPIRY_CODE,
        error_detail="Preparation deadline elapsed before provider invocation",
        shortfall_kind="pacing_capacity", evaluator_evidence={"reason_code": EXPIRY_CODE},
        next_retry_at=job.latest_safe_send_at,
    ))
    if outcome != "shortfall":
        raise RuntimeError("comment_preparation_expiry_not_settled")
    _settle_action_pacing_reservation(session, action.id, replan_same_obligation=False)
    release_source_pacing_admissions_before_gateway(session, action)


def _lock_obligation_owners(session: Session, action: Action) -> None:
    obligation_id = str((action.payload or {}).get("comment_fulfillment_obligation_id") or "")
    queries = (
        select(FulfillmentObligationProjection).where(
            FulfillmentObligationProjection.obligation_type == action.obligation_type,
            FulfillmentObligationProjection.obligation_id == action.obligation_id,
        ),
        select(CommentFulfillmentObligation).where(CommentFulfillmentObligation.id == obligation_id),
    )
    for query in queries:
        row = session.scalar(query.with_for_update().execution_options(populate_existing=True))
        if row is not None and (row.tenant_id, row.task_id) != (action.tenant_id, action.task_id):
            raise RuntimeError("comment_preparation_expiry_obligation_scope_mismatch")
        if row is not None and row.task_lifecycle_epoch not in {None, action.task_lifecycle_epoch}:
            raise RuntimeError("comment_preparation_expiry_obligation_epoch_mismatch")

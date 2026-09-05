"""Retire terminal content bindings only with proven nonexecution."""
from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.models import Action, AiContentWindowPlan, AiContentWindowPlanSlot
from app.models import GenerationJob

from .ai_content_window_evidence import content_action_proven_unsent


TERMINAL_UNSENT_ACTION_STATES = ("failed", "skipped", "cancelled")


def terminal_pre_gateway_slot_query(slot_states, job_states):
    return select(AiContentWindowPlanSlot).join(
        GenerationJob, GenerationJob.id == AiContentWindowPlanSlot.claimed_by_job_id,
    ).where(
        AiContentWindowPlanSlot.state.in_(slot_states),
        GenerationJob.state.in_(job_states),
    )


def retire_pre_gateway_bound_slot(
    session: Session, *, obligation_type: str, obligation_id: str,
) -> bool:
    owner = session.execute(_unexecuted_bound_slot_query().where(
        AiContentWindowPlanSlot.obligation_type == obligation_type,
        AiContentWindowPlanSlot.obligation_id == obligation_id,
    ).with_for_update()).one_or_none()
    if owner is None:
        return False
    slot, action = owner
    if not content_action_proven_unsent(session, action):
        return False
    slot.state = "invalidated"
    slot.claimed_by_job_id = None
    slot.lease_expires_at = None
    slot.version += 1
    session.flush()
    return True


def _unexecuted_bound_slot_query():
    return select(AiContentWindowPlanSlot, Action).join(
        GenerationJob, GenerationJob.id == AiContentWindowPlanSlot.claimed_by_job_id,
    ).join(AiContentWindowPlan, AiContentWindowPlan.id == AiContentWindowPlanSlot.plan_id).join(
        Action, and_(
            Action.tenant_id == GenerationJob.tenant_id,
            Action.task_id == GenerationJob.task_id,
            Action.task_lifecycle_epoch == GenerationJob.task_lifecycle_epoch,
            Action.obligation_type == GenerationJob.obligation_type,
            Action.obligation_id == GenerationJob.obligation_id,
            Action.payload["generation_job_id"].as_string() == GenerationJob.id,
        ),
    ).where(
        AiContentWindowPlanSlot.state == "gateway_bound",
        AiContentWindowPlanSlot.obligation_type == GenerationJob.obligation_type,
        AiContentWindowPlanSlot.obligation_id == GenerationJob.obligation_id,
        AiContentWindowPlan.tenant_id == GenerationJob.tenant_id,
        AiContentWindowPlan.task_id == GenerationJob.task_id,
        AiContentWindowPlan.task_lifecycle_epoch == GenerationJob.task_lifecycle_epoch,
        GenerationJob.window_slot_id == AiContentWindowPlanSlot.id,
        GenerationJob.state == "ready",
        GenerationJob.generation_stage == "gateway_bound",
        Action.task_type == "group_ai_chat",
        Action.action_type == "send_message",
        Action.status.in_(TERMINAL_UNSENT_ACTION_STATES),
        Action.payload["ai_generation_status"].as_string() == "ready",
    )

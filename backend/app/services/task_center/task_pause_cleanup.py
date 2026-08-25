from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Action,
    AiContentWindowPlan,
    AiContentWindowPlanSlot,
    ExecutionAttempt,
    GenerationJob,
    Task,
)

from .ai_content_runtime import invalidate_pre_gateway_window_slot


OPEN_ACTION_STATES = (
    "pending",
    "claiming",
    "executing",
    "retryable_failed",
    "unknown_after_send",
)
OPEN_JOB_STATES = ("pending", "generating", "unknown")
PRE_GATEWAY_SLOT_STATES = ("claimed", "candidate_ready")


def lock_and_has_ambiguous_group_ai_work(session: Session, task: Task) -> bool:
    actions = list(
        session.scalars(
            select(Action)
            .where(
                Action.task_id == task.id,
                Action.status.in_(OPEN_ACTION_STATES),
            )
            .with_for_update()
        )
    )
    if any(action.status == "unknown_after_send" for action in actions):
        return True
    if _gateway_attempt_started(session, [action.id for action in actions]):
        return True
    jobs = list(
        session.scalars(
            select(GenerationJob)
            .where(
                GenerationJob.task_id == task.id,
                GenerationJob.state.in_(OPEN_JOB_STATES),
            )
            .with_for_update()
        )
    )
    return _gateway_slot_bound(session, [job.window_slot_id for job in jobs])


def _gateway_attempt_started(session: Session, action_ids: list[str]) -> bool:
    if not action_ids:
        return False
    attempts = list(
        session.scalars(
            select(ExecutionAttempt)
            .where(ExecutionAttempt.action_id.in_(action_ids))
            .with_for_update()
        )
    )
    return any(attempt.gateway_call_started_at is not None for attempt in attempts)


def _gateway_slot_bound(session: Session, slot_ids: list[str | None]) -> bool:
    ids = [slot_id for slot_id in slot_ids if slot_id]
    if not ids:
        return False
    slots = list(
        session.scalars(
            select(AiContentWindowPlanSlot)
            .where(AiContentWindowPlanSlot.id.in_(ids))
            .with_for_update()
        )
    )
    return any(slot.state == "gateway_bound" for slot in slots)


def cancel_open_generation_jobs(session: Session, task: Task) -> int:
    jobs = list(
        session.scalars(
            select(GenerationJob).where(
                GenerationJob.task_id == task.id,
                GenerationJob.state.in_(OPEN_JOB_STATES),
            )
        )
    )
    _invalidate_task_pre_gateway_slots(session, task)
    for job in jobs:
        job.state = "cancelled"
        job.generation_stage = "cancelled_by_task_lifecycle"
        job.generation_owner_id = ""
        job.lease_expires_at = None
        job.next_retry_at = None
        job.evaluator_evidence = {
            **dict(job.evaluator_evidence or {}),
            "invalidation_reason": "task_lifecycle_paused",
        }
        job.job_version = int(job.job_version or 1) + 1
    return len(jobs)


def _invalidate_task_pre_gateway_slots(session: Session, task: Task) -> int:
    statement = (
        select(AiContentWindowPlanSlot)
        .join(AiContentWindowPlan, AiContentWindowPlan.id == AiContentWindowPlanSlot.plan_id)
        .where(
            AiContentWindowPlan.task_id == task.id,
            AiContentWindowPlanSlot.state.in_(PRE_GATEWAY_SLOT_STATES),
        )
    )
    if session.get_bind().dialect.name != "sqlite":
        statement = statement.with_for_update(of=AiContentWindowPlanSlot)
    slots = list(session.scalars(statement))
    return sum(invalidate_pre_gateway_window_slot(slot) for slot in slots)


__all__ = [
    "cancel_open_generation_jobs",
    "lock_and_has_ambiguous_group_ai_work",
]

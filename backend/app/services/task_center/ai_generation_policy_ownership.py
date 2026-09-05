from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AiContentPolicyVersion,
    AiContentWindowPlan,
    AiContentWindowPlanSlot,
    GenerationJob,
    Task,
    TaskAiContentPolicyBinding,
)

from .ai_content_job_binding_error import AiContentJobBindingError


@dataclass(frozen=True)
class GenerationPolicySnapshot:
    binding: TaskAiContentPolicyBinding
    policy: AiContentPolicyVersion
    window_slot: AiContentWindowPlanSlot | None = None

    @property
    def frozen_route(self) -> str:
        return self.window_slot.context_route if self.window_slot is not None else ""


def generation_policy_snapshots(
    session: Session, task: Task, jobs: tuple[GenerationJob, ...],
) -> dict[str, GenerationPolicySnapshot]:
    windows = _job_windows(session, task, jobs=jobs)
    revisions = {plan.task_config_revision for _slot, plan in windows.values()}
    if any(not job.window_slot_id for job in jobs):
        revisions.add(task.config_revision)
    policies = _policy_revisions(session, task, revisions=revisions)
    return {
        job.id: _job_policy(task, job, windows=windows, policies=policies)
        for job in jobs
    }


def _job_windows(session: Session, task: Task, *, jobs: tuple) -> dict:
    slot_ids = {job.window_slot_id for job in jobs if job.window_slot_id}
    rows = session.execute(
        select(AiContentWindowPlanSlot, AiContentWindowPlan)
        .join(AiContentWindowPlan, AiContentWindowPlan.id == AiContentWindowPlanSlot.plan_id)
        .where(AiContentWindowPlanSlot.id.in_(slot_ids))
    ).all() if slot_ids else ()
    windows = {slot.id: (slot, plan) for slot, plan in rows}
    for job in jobs:
        _validate_job_scope(task, job)
        if job.window_slot_id:
            _validate_window(task, job, window=windows.get(job.window_slot_id))
    return windows


def _validate_job_scope(task: Task, job: GenerationJob) -> None:
    if (job.tenant_id, job.task_id, job.task_lifecycle_epoch) != (
        task.tenant_id, task.id, task.task_lifecycle_epoch,
    ):
        raise AiContentJobBindingError("generation_policy_job_scope_mismatch")


def _validate_window(task: Task, job: GenerationJob, *, window) -> None:
    if window is None:
        raise AiContentJobBindingError("generation_policy_window_missing")
    slot, plan = window
    if (plan.tenant_id, plan.task_id, plan.task_lifecycle_epoch) != (
        task.tenant_id, task.id, job.task_lifecycle_epoch,
    ):
        raise AiContentJobBindingError("generation_policy_window_scope_mismatch")
    if (slot.claimed_by_job_id, slot.obligation_type, slot.obligation_id) != (
        job.id, job.obligation_type, job.obligation_id,
    ):
        raise AiContentJobBindingError("generation_policy_window_binding_mismatch")
    if plan.plan_hash != job.window_plan_hash:
        raise AiContentJobBindingError("generation_policy_window_hash_mismatch")


def _policy_revisions(session: Session, task: Task, *, revisions: set) -> dict:
    rows = session.execute(
        select(TaskAiContentPolicyBinding, AiContentPolicyVersion)
        .outerjoin(AiContentPolicyVersion,
                   AiContentPolicyVersion.id == TaskAiContentPolicyBinding.policy_version_id)
        .where(
            TaskAiContentPolicyBinding.tenant_id == task.tenant_id,
            TaskAiContentPolicyBinding.task_id == task.id,
            TaskAiContentPolicyBinding.task_lifecycle_epoch == task.task_lifecycle_epoch,
            TaskAiContentPolicyBinding.task_config_revision.in_(revisions),
        )
    ).all()
    policies = {}
    for binding, policy in rows:
        if policy is None or not policy.policy_hash or policy.tenant_id != task.tenant_id:
            raise AiContentJobBindingError("ai_content_policy_snapshot_missing")
        policies[binding.task_config_revision] = GenerationPolicySnapshot(binding, policy)
    return policies


def _job_policy(task: Task, job: GenerationJob, *, windows: dict, policies: dict):
    if not job.window_slot_id:
        return _required_policy(policies, task.config_revision)
    slot, plan = windows[job.window_slot_id]
    snapshot = _required_policy(policies, plan.task_config_revision)
    if not (plan.content_policy_hash == job.content_policy_hash == snapshot.policy.policy_hash):
        raise AiContentJobBindingError("generation_policy_hash_mismatch")
    if not (job.task_binding_hash == job.task_direction_snapshot_hash == snapshot.binding.evidence_hash):
        raise AiContentJobBindingError("generation_policy_binding_hash_mismatch")
    return GenerationPolicySnapshot(snapshot.binding, snapshot.policy, slot)


def _required_policy(policies: dict, revision: int) -> GenerationPolicySnapshot:
    snapshot = policies.get(revision)
    if snapshot is None:
        raise AiContentJobBindingError("task_ai_content_policy_binding_missing")
    return snapshot

"""Bind current generation identity and deadlines without historical approval."""
from datetime import timedelta

from sqlalchemy import select

from app.models import GenerationJob, GenerationTimingBinding
from app.services._common import _now
from app.timezone import as_beijing

from .engagement_timing_measurements import timing_hash
from .generation_deadlines import minimum_generation_deadline
from .generation_timing_path import generation_execution_path
from .generation_invocation_budget import MAX_LLM_INVOCATION_SECONDS, TIMING_CONFIG_KEY


PRE_SEND_REVIEW_SECONDS = 1
TIMING_POLICY = "deadline_only_v1"


def bind_generation_timing_config(session, task, *, work: tuple, config: dict, deadline_at, requires_provider: bool = True) -> dict:
    if config.get("engagement_contract_version") != "unified_engagement_v1":
        return config
    if not config.get("ai_content_route_v2_enabled"):
        return config
    if not work:
        raise ValueError("generation_timing_frozen_jobs_or_routes_missing")
    now_value = as_beijing(_now())
    with session.begin_nested():
        snapshots = [_bind_job(session, task, job=job, lane=lane, config=config, now_value=now_value,
                               deadline_at=deadline_at, requires_provider=requires_provider)
                     for job, lane in sorted(work, key=lambda item: item[0].id)]
    return {**config, TIMING_CONFIG_KEY: {
        "version": "generation_timing_v1", "timing_policy": TIMING_POLICY, "bindings": snapshots,
        "provider_calls_allowed": requires_provider,
        "candidate_ready_deadline_at": min(item["candidate_ready_deadline_at"] for item in snapshots),
        "llm_timeout_ceiling_seconds": min(item["llm_timeout_ceiling_seconds"] for item in snapshots),
    }}


def _bind_job(session, task, *, job, lane, config, now_value, deadline_at, requires_provider) -> dict:
    session.flush()
    locked = session.scalar(select(GenerationJob).where(GenerationJob.id == job.id).with_for_update().execution_options(populate_existing=True))
    if locked is None or (locked.tenant_id, locked.task_id, locked.task_lifecycle_epoch) != (task.tenant_id, task.id, task.task_lifecycle_epoch):
        raise ValueError("generation_timing_job_scope_mismatch")
    deadline = minimum_generation_deadline((locked.latest_safe_send_at, deadline_at))
    if deadline is None:
        raise ValueError("generation_timing_deadline_missing")
    path = generation_execution_path(locked, adapter=task.type, config=config)
    path_hash = timing_hash(path.snapshot(adapter=task.type, lane=lane))
    binding = session.get(GenerationTimingBinding, job.id)
    if binding is None and not requires_provider:
        raise ValueError("generation_timing_recovery_binding_missing")
    if binding is None:
        binding = GenerationTimingBinding(
            generation_job_id=job.id, tenant_id=task.tenant_id, task_id=task.id,
            task_lifecycle_epoch=task.task_lifecycle_epoch, adapter=task.type, lane=lane,
            execution_path_hash=path_hash, llm_timeout_ceiling_seconds=MAX_LLM_INVOCATION_SECONDS,
            bound_at=now_value, bound_send_deadline_at=deadline,
        )
        session.add(binding)
        session.flush()
    expected = (task.tenant_id, task.id, task.task_lifecycle_epoch, task.type, lane, path_hash)
    actual = (binding.tenant_id, binding.task_id, binding.task_lifecycle_epoch, binding.adapter, binding.lane, binding.execution_path_hash)
    if expected != actual:
        raise ValueError("generation_timing_binding_changed")
    deadline = minimum_generation_deadline((deadline, binding.bound_send_deadline_at))
    result = _deadline_snapshot(binding, deadline=deadline, now_value=now_value)
    binding.bound_send_deadline_at = deadline
    session.flush()
    return {**result, "generation_sequence": locked.generation_sequence,
            "generation_lease_epoch": locked.generation_lease_epoch, "generation_owner_id": locked.generation_owner_id}


def _deadline_snapshot(binding, *, deadline, now_value) -> dict:
    ready_deadline = deadline - timedelta(seconds=PRE_SEND_REVIEW_SECONDS)
    if (ready_deadline - now_value).total_seconds() < 1:
        raise ValueError("generation_timing_preparation_deadline_missed")
    ceiling = binding.llm_timeout_ceiling_seconds
    if type(ceiling) is not int or not 0 < ceiling <= MAX_LLM_INVOCATION_SECONDS:
        raise ValueError("generation_timing_llm_ceiling_invalid")
    return {
        "tenant_id": binding.tenant_id, "task_id": binding.task_id, "task_lifecycle_epoch": binding.task_lifecycle_epoch,
        "generation_job_id": binding.generation_job_id, "timing_policy": TIMING_POLICY,
        "execution_path_hash": binding.execution_path_hash, "derived_at": now_value.isoformat(),
        "candidate_ready_deadline_at": ready_deadline.isoformat(),
        "llm_timeout_ceiling_seconds": ceiling,
    }

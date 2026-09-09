"""Prove a new group candidate may coexist with locally terminated unknown HTTP."""
from datetime import datetime, timedelta

from sqlalchemy import select

from app.ai_gateway import canonical_ai_model_identity
from app.models import GenerationTimingBinding, ProviderHttpExchange, ProviderHttpExchangeJob, Task
from app.services._common import _now
from app.timezone import as_beijing

from .generation_timing_binding import PRE_SEND_REVIEW_SECONDS


UNIFIED_CONTRACT = "unified_engagement_v1"


def consider_unknown_candidate(config: dict | None, *, route_bound: bool, has_more: bool) -> bool:
    config = config or {}
    return (has_more and route_bound and config.get("_ai_group_emergency_enabled") is True
            and config.get("engagement_contract_version") == UNIFIED_CONTRACT)


def allow_group_unknown_failover(session, scope, *, jobs, conflict_ids) -> bool:
    if not scope.emergency_enabled or not conflict_ids:
        return False
    task = session.get(Task, jobs[0].task_id)
    config = task.type_config or {}
    if (task.type != "group_ai_chat" or config.get("engagement_contract_version") != UNIFIED_CONTRACT
            or config.get("emergency_fallback_enabled", True) is not True):
        return False
    if not all(_approved_route(job, scope) and _before_original_deadline(session, job, scope) for job in jobs):
        return False
    exchanges = list(session.scalars(select(ProviderHttpExchange).where(ProviderHttpExchange.id.in_(conflict_ids))))
    if not all(_terminated_distinct_exchange(exchange, scope, jobs[0]) for exchange in exchanges):
        return False
    bindings = {item["generation_job_id"]: item["execution_path_hash"] for item in scope.job_bindings}
    links = session.execute(select(ProviderHttpExchangeJob.generation_job_id,
        ProviderHttpExchangeJob.execution_path_hash).where(ProviderHttpExchangeJob.exchange_id.in_(conflict_ids)))
    return all(bindings.get(job_id) == path_hash for job_id, path_hash in links)


def _approved_route(job, scope) -> bool:
    route = (job.provider_route_snapshots or {}).get(scope.purpose) or {}
    identity = (route.get("route_set_id"), route.get("revision"), route.get("content_hash"))
    expected = (scope.route_set_id, scope.route_set_revision, scope.route_set_hash)
    if not all(expected) or identity != expected:
        return False
    return any(
        (item.get("provider_id"), canonical_ai_model_identity(str(item.get("model_name") or "")))
        == (scope.provider_id, canonical_ai_model_identity(scope.model_name))
        for item in route.get("candidates") or ()
    )


def _before_original_deadline(session, job, scope) -> bool:
    binding = session.get(GenerationTimingBinding, job.id)
    expected = next(item for item in scope.job_bindings if item["generation_job_id"] == job.id)
    frozen_deadline = datetime.fromisoformat(expected["candidate_ready_deadline_at"])
    return as_beijing(_now()) < min(
        as_beijing(frozen_deadline),
        as_beijing(binding.bound_send_deadline_at) - timedelta(seconds=PRE_SEND_REVIEW_SECONDS),
        as_beijing(job.latest_safe_send_at) - timedelta(seconds=PRE_SEND_REVIEW_SECONDS),
    )


def _terminated_distinct_exchange(exchange, scope, job) -> bool:
    owner = (job.tenant_id, job.task_id, job.task_lifecycle_epoch)
    if (exchange.tenant_id, exchange.task_id, exchange.task_lifecycle_epoch) != owner:
        return False
    if exchange.outcome != "unknown" or exchange.local_termination_confirmed is not True:
        return False
    if exchange.logical_request_id == scope.logical_request_id:
        return False
    same_candidate = (exchange.provider_id, canonical_ai_model_identity(exchange.model_name)) == (
        scope.provider_id, canonical_ai_model_identity(scope.model_name))
    return exchange.purpose != scope.purpose or (not same_candidate and _later_candidate(job, exchange, scope))


def _later_candidate(job, exchange, scope) -> bool:
    candidates = (job.provider_route_snapshots or {}).get(scope.purpose, {}).get("candidates") or ()
    priorities = {
        (item["provider_id"], canonical_ai_model_identity(item["model_name"])): item["priority"]
        for item in candidates
    }
    previous = priorities.get((exchange.provider_id, canonical_ai_model_identity(exchange.model_name)))
    current = priorities.get((scope.provider_id, canonical_ai_model_identity(scope.model_name)))
    return previous is not None and current is not None and current > previous

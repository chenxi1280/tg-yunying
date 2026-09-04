"""Durable physical exchanges; not a substitute for task budget/capacity admission."""
from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import DBAPIError

from app.ai_transport_errors import AiProviderResultUnknown
from app.models import GenerationJob, GenerationTimingBinding, ProviderHttpExchange, ProviderHttpExchangeJob, Task
from app.services._common import _now
from app.timezone import as_beijing
from .provider_admission import ProviderAdmissionBlocked
from .generation_provider_lineage import UNRESOLVED_EXCHANGE_STATES, unresolved_exchange_statement


EXCHANGE_ADMISSION_RETRY_SECONDS = 1


@dataclass(frozen=True)
class ExchangeScope:
    job_bindings: tuple[dict, ...]
    provider_id: int
    model_name: str
    purpose: str
    logical_request_id: str


def start_exchange(session_factory, scope: ExchangeScope, *, chain_id: str, request_hash: str) -> str:
    try:
        return _start_exchange(session_factory, scope, chain_id=chain_id, request_hash=request_hash)
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) == "55P03":
            raise ProviderAdmissionBlocked("provider-http-ledger", EXCHANGE_ADMISSION_RETRY_SECONDS,
                reason="provider_exchange_admission_busy") from exc
        raise


def _start_exchange(session_factory, scope, *, chain_id, request_hash):
    with session_factory() as session, session.begin():
        jobs = _lock_jobs(session, scope)
        job_ids = tuple(job.id for job in jobs)
        conflicts = unresolved_exchange_statement(jobs).where(
            (ProviderHttpExchange.chain_id != chain_id) | (ProviderHttpExchange.outcome != "response_received")
            | ProviderHttpExchangeJob.generation_job_id.not_in(job_ids),
        )
        if session.scalar(conflicts.limit(1)):
            raise AiProviderResultUnknown("provider_http_previous_exchange_unresolved")
        exchange_id = str(uuid4())
        first = jobs[0]
        session.add(ProviderHttpExchange(
            id=exchange_id, chain_id=chain_id, tenant_id=first.tenant_id, task_id=first.task_id,
            task_lifecycle_epoch=first.task_lifecycle_epoch, provider_id=scope.provider_id,
            logical_request_id=scope.logical_request_id, model_name=scope.model_name, purpose=scope.purpose,
            request_hash=request_hash, outcome="started", started_at=_now(),
        ))
        session.flush()
        for binding in scope.job_bindings:
            session.add(ProviderHttpExchangeJob(exchange_id=exchange_id,
                generation_job_id=binding["generation_job_id"], execution_path_hash=binding["execution_path_hash"]))
    return exchange_id


def _lock_jobs(session, scope):
    if not scope.job_bindings or not scope.logical_request_id or len(scope.logical_request_id) > 200:
        raise ValueError("provider_http_scope_missing")
    ids = tuple(sorted(item["generation_job_id"] for item in scope.job_bindings))
    if len(set(ids)) != len(ids):
        raise ValueError("provider_http_duplicate_job_scope")
    first = scope.job_bindings[0]
    owner = (first.get("tenant_id"), first.get("task_id"), first.get("task_lifecycle_epoch"))
    task = session.scalar(select(Task).where(Task.id == owner[1]).with_for_update(read=True, nowait=True))
    if task is None or (task.tenant_id, task.id, task.task_lifecycle_epoch) != owner or task.status != "running":
        raise ValueError("provider_http_task_owner_stale")
    jobs = list(session.scalars(select(GenerationJob).where(GenerationJob.id.in_(ids)).order_by(GenerationJob.id).with_for_update(nowait=True)))
    if len(jobs) != len(ids):
        raise ValueError("provider_http_job_missing")
    bindings = {item.generation_job_id: item for item in session.scalars(
        select(GenerationTimingBinding).where(GenerationTimingBinding.generation_job_id.in_(ids)))}
    expected = {item["generation_job_id"]: item for item in scope.job_bindings}
    for job in jobs:
        _validate_job(job, binding=bindings.get(job.id), expected=expected[job.id], owner=owner)
    return jobs


def _validate_job(job, *, binding, expected, owner):
    if binding is None or (job.tenant_id, job.task_id, job.task_lifecycle_epoch) != owner:
        raise ValueError("provider_http_job_scope_mismatch")
    if (binding.tenant_id, binding.task_id, binding.task_lifecycle_epoch) != owner:
        raise ValueError("provider_http_timing_scope_mismatch")
    if binding.execution_path_hash != expected["execution_path_hash"]:
        raise ValueError("provider_http_timing_changed")
    if (expected.get("tenant_id"), expected.get("task_id"), expected.get("task_lifecycle_epoch")) != owner:
        raise ValueError("provider_http_expected_scope_mismatch")
    actual = (job.generation_sequence, job.generation_lease_epoch, job.generation_owner_id)
    wanted = (expected.get("generation_sequence"), expected.get("generation_lease_epoch"), expected.get("generation_owner_id"))
    if actual != wanted or not job.generation_owner_id or job.state != "generating":
        raise ValueError("provider_http_generation_owner_stale")
    if job.lease_expires_at is None or as_beijing(job.lease_expires_at) <= as_beijing(_now()):
        raise ValueError("provider_http_generation_lease_expired")


def receive_exchange(session_factory, exchange_id: str, *, outcome: str, **facts) -> None:
    if outcome not in {"response_received", "not_started", "unknown"}:
        raise ValueError("provider_http_outcome_invalid")
    try:
        with session_factory() as session, session.begin():
            result = session.execute(update(ProviderHttpExchange).where(
                ProviderHttpExchange.id == exchange_id, ProviderHttpExchange.outcome == "started",
            ).values(outcome=outcome, received_at=_now(), **facts))
            if result.rowcount != 1:
                raise ValueError("provider_http_response_cas_conflict")
    except Exception as exc:
        raise AiProviderResultUnknown("provider_http_response_persistence_unproven") from exc


def settle_provider_exchanges(session, config: dict, *, provider_id: int, request_id: str, outcome: str, chain_id: str) -> None:
    if config.get("engagement_contract_version") != "unified_engagement_v1":
        return
    bindings = dict(config.get("_ai_execution_timing") or {}).get("bindings") or ()
    if not bindings or not chain_id:
        return  # No tracked exchange can start without a timing binding.
    ids = [item["generation_job_id"] for item in bindings]
    linked = select(ProviderHttpExchangeJob.exchange_id).where(ProviderHttpExchangeJob.generation_job_id.in_(ids))
    session.execute(update(ProviderHttpExchange).where(
        ProviderHttpExchange.id.in_(linked), ProviderHttpExchange.logical_request_id == request_id,
        ProviderHttpExchange.chain_id == chain_id,
        ProviderHttpExchange.provider_id == provider_id, ProviderHttpExchange.outcome == "response_received",
    ).values(outcome="unknown" if outcome == "provider_result_unknown" else "settled", settled_at=_now()))

"""Stable obligation identity survives generation/source/epoch successors."""
from sqlalchemy import select, tuple_

from app.models import GenerationJob, ProviderHttpExchange, ProviderHttpExchangeJob


UNRESOLVED_EXCHANGE_STATES = ("started", "response_received", "unknown")


def generation_lineage(job):
    return (job.tenant_id, job.task_id, job.obligation_type, job.obligation_id)


def _lineage_columns():
    return (GenerationJob.tenant_id, GenerationJob.task_id,
            GenerationJob.obligation_type, GenerationJob.obligation_id)


def _lineage_predicate(jobs):
    return tuple_(*_lineage_columns()).in_({generation_lineage(job) for job in jobs})


def unresolved_exchange_statement(jobs):
    return select(ProviderHttpExchange.id).join(ProviderHttpExchangeJob).join(
        GenerationJob, GenerationJob.id == ProviderHttpExchangeJob.generation_job_id,
    ).where(_lineage_predicate(jobs), ProviderHttpExchange.outcome.in_(UNRESOLVED_EXCHANGE_STATES))


def unresolved_generation_lineages(session, jobs):
    physical = select(*_lineage_columns()).join(
        ProviderHttpExchangeJob, ProviderHttpExchangeJob.generation_job_id == GenerationJob.id,
    ).join(ProviderHttpExchange).where(
        _lineage_predicate(jobs), ProviderHttpExchange.outcome.in_(UNRESOLVED_EXCHANGE_STATES),
    )
    unknown = select(*_lineage_columns()).where(_lineage_predicate(jobs), GenerationJob.state == "unknown")
    return {tuple(row) for row in session.execute(physical.union(unknown))}

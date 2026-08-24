from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    Action,
    AiProviderAttempt,
    FulfillmentRemoteFact,
    FulfillmentShortfallFact,
    GenerationJob,
    Task,
)


def ai_runtime_diagnostics(session: Session, task: Task) -> dict:
    job_filter = GenerationJob.task_id == task.id
    action_filter = (
        Action.task_id == task.id,
        Action.task_type == "group_ai_chat",
        Action.action_type == "send_message",
    )
    return {
        "generation_stage_counts": _grouped_counts(
            session, GenerationJob.generation_stage, job_filter,
        ),
        "shortfall_counts": _grouped_counts(
            session, FulfillmentShortfallFact.kind,
            FulfillmentShortfallFact.task_id == task.id,
        ),
        "dialogue_chain_state_counts": _dialogue_counts(session, action_filter),
        "token_ledger": _token_ledger(session, task.id),
        "conversion_funnel": _conversion_funnel(session, task.id, action_filter),
        "active_revisions": _active_revisions(session, task.id),
    }


def _grouped_counts(session: Session, column, *filters) -> dict[str, int]:
    rows = session.execute(
        select(column, func.count()).where(*filters).group_by(column)
    ).all()
    return {str(key or ""): int(count or 0) for key, count in rows if key}


def _dialogue_counts(session: Session, filters: tuple) -> dict[str, int]:
    state = Action.payload["dialogue_chain_state"].as_string()
    return _grouped_counts(session, state, *filters, func.coalesce(state, "") != "")


def _token_ledger(session: Session, task_id: str) -> dict:
    totals = session.execute(select(
        func.count(AiProviderAttempt.id),
        func.coalesce(func.sum(AiProviderAttempt.prompt_tokens), 0),
        func.coalesce(func.sum(AiProviderAttempt.completion_tokens), 0),
        func.coalesce(func.sum(AiProviderAttempt.cached_tokens), 0),
        func.coalesce(func.sum(AiProviderAttempt.cost_amount), 0),
    ).join(GenerationJob, GenerationJob.id == AiProviderAttempt.generation_job_id).where(
        GenerationJob.task_id == task_id,
    )).one()
    job_filter = GenerationJob.task_id == task_id
    return {
        "attempt_count": int(totals[0] or 0),
        "input_tokens": int(totals[1] or 0),
        "output_tokens": int(totals[2] or 0),
        "cache_tokens": int(totals[3] or 0),
        "cost_amount": round(float(totals[4] or 0), 8),
        "outcome_counts": _attempt_counts(session, AiProviderAttempt.outcome, job_filter),
        "purpose_counts": _attempt_counts(session, AiProviderAttempt.purpose, job_filter),
    }


def _attempt_counts(session: Session, column, job_filter) -> dict[str, int]:
    rows = session.execute(
        select(column, func.count()).join(
            GenerationJob, GenerationJob.id == AiProviderAttempt.generation_job_id,
        ).where(job_filter).group_by(column)
    ).all()
    return {str(key): int(count or 0) for key, count in rows if key}


def _conversion_funnel(session: Session, task_id: str, action_filter: tuple) -> dict:
    ready = Action.payload["ai_generation_status"].as_string() == "ready"
    return {
        "generation_jobs": _count(session, GenerationJob.id, GenerationJob.task_id == task_id),
        "accepted_candidates": _count(
            session, GenerationJob.id,
            GenerationJob.task_id == task_id, GenerationJob.candidate_hash != "",
        ),
        "provider_attempts": _provider_attempt_count(session, task_id),
        "ready_actions": _count(session, Action.id, *action_filter, ready),
        "telegram_remote_success": _count(
            session, FulfillmentRemoteFact.fact_id,
            FulfillmentRemoteFact.task_id == task_id,
            FulfillmentRemoteFact.fact_kind == "remote_message_observed",
        ),
    }


def _provider_attempt_count(session: Session, task_id: str) -> int:
    value = session.scalar(select(func.count(AiProviderAttempt.id)).join(
        GenerationJob, GenerationJob.id == AiProviderAttempt.generation_job_id,
    ).where(GenerationJob.task_id == task_id))
    return int(value or 0)


def _count(session: Session, column, *filters) -> int:
    return int(session.scalar(select(func.count(column)).where(*filters)) or 0)


def _active_revisions(session: Session, task_id: str) -> dict[str, list]:
    return {
        "route": _distinct_text(session, GenerationJob.context_route, task_id),
        "prompt": _distinct_text(session, GenerationJob.prompt_contract_version, task_id),
        "voice": _distinct_text(session, GenerationJob.voice_profile_version, task_id),
        "provider": _distinct_positive(
            session, GenerationJob.provider_route_set_revision, task_id,
        ),
    }


def _distinct_text(session: Session, column, task_id: str) -> list[str]:
    rows = session.scalars(select(column).where(
        GenerationJob.task_id == task_id, column != "",
    ).distinct())
    return sorted(rows)


def _distinct_positive(session: Session, column, task_id: str) -> list[int]:
    rows = session.scalars(select(column).where(
        GenerationJob.task_id == task_id, column > 0,
    ).distinct())
    return sorted(int(value) for value in rows)


__all__ = ["ai_runtime_diagnostics"]

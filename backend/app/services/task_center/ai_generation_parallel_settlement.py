from __future__ import annotations

from datetime import timedelta

from app.services._common import _now

from .ai_generation_claim_lifecycle import (
    defer_unprepared_batch,
    persisted_generation_outcome,
    release_prepared_batch,
    release_unprepared_batch,
)
from .ai_generation_contract_errors import (
    GenerationContractErrorTarget,
    terminate_generation_contract_error,
)
from .ai_generation_parallel import (
    defer_parallel_generation,
    finish_generation_job,
    settle_deferred_parallel_claim,
)
from .ai_generation_worker_types import GenerationOutcome, SequentialClaim


def settle_sequential_outcome(
    session_factory,
    claim: SequentialClaim,
    outcome: GenerationOutcome,
) -> int:
    if outcome.provider_deferred is not None:
        next_retry_at = _now() + timedelta(
            seconds=outcome.provider_deferred.retry_after_seconds
        )
        defer_unprepared_batch(
            session_factory, claim.owner, claim.token, next_retry_at=next_retry_at,
        )
        return claim.claimed_count
    if outcome.admission_deferred:
        release_unprepared_batch(session_factory, claim.owner, claim.token)
        return claim.claimed_count
    if outcome.failure is not None:
        _settle_sequential_failure(session_factory, claim, outcome.failure)
        return claim.claimed_count
    return release_prepared_batch(session_factory, claim.owner, claim.token)


def _settle_sequential_failure(session_factory, claim, failure: Exception) -> None:
    persisted = persisted_generation_outcome(session_factory, claim.action_id)
    if not persisted:
        terminate_generation_contract_error(
            session_factory,
            GenerationContractErrorTarget(claim.action_id, claim.owner, claim.token),
            failure,
        )
        raise failure
    if persisted != "deferred":
        release_unprepared_batch(session_factory, claim.owner, claim.token)


def settle_parallel_outcome(session_factory, claim, outcome: GenerationOutcome) -> int:
    if outcome.provider_deferred is not None:
        next_retry_at = _now() + timedelta(
            seconds=outcome.provider_deferred.retry_after_seconds
        )
        defer_parallel_generation(session_factory, claim, next_retry_at=next_retry_at)
        return 1
    if outcome.admission_deferred:
        settle_deferred_parallel_claim(session_factory, claim)
        return 1
    if outcome.failure is not None:
        _settle_failed_outcome(session_factory, claim, outcome.failure)
        return 1
    count = release_prepared_batch(session_factory, claim.owner, claim.token)
    finish_generation_job(session_factory, claim, state="ready")
    return count


def _settle_failed_outcome(session_factory, claim, failure: Exception) -> None:
    persisted = persisted_generation_outcome(session_factory, claim.action_id)
    if not persisted:
        terminate_generation_contract_error(
            session_factory,
            GenerationContractErrorTarget(
                claim.action_id, claim.owner, claim.token, claim.job_id,
            ),
            failure,
        )
        raise failure
    if persisted == "deferred":
        settle_deferred_parallel_claim(session_factory, claim)
        return
    release_unprepared_batch(session_factory, claim.owner, claim.token)
    finish_generation_job(session_factory, claim, state="failed")


__all__ = ["settle_parallel_outcome", "settle_sequential_outcome"]

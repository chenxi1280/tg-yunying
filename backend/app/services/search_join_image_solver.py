from __future__ import annotations

import threading
from concurrent.futures import Future
from datetime import UTC, datetime
from time import monotonic
from typing import Any

from app.integrations.telegram.search_join import (
    ImageVerificationDecision,
    ImageVerificationRequest,
    ImageVerificationRuntimeContractError,
    ImageVerificationVote,
)

from .image_verification_client import (
    ImageVerificationWorkerClient,
    RemoteOcrResult,
)
from .image_verification_runtime import (
    SLOT_POLL_SECONDS,
    ImageVerificationPolicy,
    ImageVerificationRuntime,
    TimedResult,
    VerificationBudget,
)
from .image_verification_sources import (
    harvest_ocr_futures,
    remote_ocr_votes,
    submit_available_ocr,
    vote_with_timing,
)

MIN_IMAGE_VERIFICATION_CONFIDENCE = 0.70
IMAGE_VERIFICATION_SOURCE_COUNT = 3
IMAGE_VERIFICATION_CONSENSUS_COUNT = 2


def collect_search_join_image_votes(
    credentials: Any,
    request: ImageVerificationRequest,
    *,
    action_id: str,
    policy: ImageVerificationPolicy,
    runtime: ImageVerificationRuntime,
) -> ImageVerificationDecision:
    calibration_error = policy.calibration_error()
    if calibration_error:
        raise ImageVerificationRuntimeContractError(
            "verification_deadline_not_calibrated",
            calibration_error,
        )
    budget = VerificationBudget.create(
        policy,
        observed_at=request.challenge_observed_at,
        observed_monotonic=request.challenge_observed_monotonic,
    )
    runtime.record_challenge()
    if policy.ocr_backend == "remote":
        return _run_remote_sources(
            credentials,
            request,
            action_id,
            policy,
            runtime,
            budget,
        )
    return _run_local_sources(
        credentials,
        request,
        policy,
        runtime,
        budget,
    )


def _run_remote_sources(
    credentials: Any,
    request: ImageVerificationRequest,
    action_id: str,
    policy: ImageVerificationPolicy,
    runtime: ImageVerificationRuntime,
    budget: VerificationBudget,
) -> ImageVerificationDecision:
    client = ImageVerificationWorkerClient(policy)
    remote_future = runtime.submit_remote_ocr(
        lambda: client.recognize(action_id, request, budget)
    )
    model_source = _image_provider_label(credentials)
    model_future: Future[ImageVerificationVote] | None = None
    model_reason = ""
    votes: dict[str, ImageVerificationVote] = {}
    while budget.remaining_seconds() > 0:
        decision = _harvest_remote_ocr(
            remote_future, request, votes, budget
        )
        if remote_future.done():
            if decision is not None:
                return _finalize_decision(
                    decision.answer, votes, model_source, model_future,
                    model_reason, policy, budget,
                )
            _raise_if_local_votes_unsafe(votes)
        if model_future is None:
            model_reason = _remote_model_start_reason(votes, budget)
            if model_reason:
                model_future = _start_model(
                    credentials, request, policy, runtime, budget,
                    model_source, model_reason,
                )
        decision = _harvest_model(model_future, votes, model_source)
        if decision is not None:
            return _finalize_decision(
                decision.answer, votes, model_source, model_future,
                model_reason, policy, budget,
            )
        _raise_if_all_sources_diverge(votes, model_source)
        _wait_for_remote_progress(remote_future, model_future, budget)
    raise ImageVerificationRuntimeContractError(
        "verification_deadline_exceeded",
        "remote OCR/model exceeded callback deadline",
        _deadline_votes(votes, {}, model_source, model_future),
    )


def _harvest_remote_ocr(
    future: Future[RemoteOcrResult],
    request: ImageVerificationRequest,
    votes: dict[str, ImageVerificationVote],
    budget: VerificationBudget,
) -> ImageVerificationDecision | None:
    local_sources = ("rapidocr", "ddddocr")
    if not future.done() or any(source in votes for source in local_sources):
        return None
    try:
        votes.update(remote_ocr_votes(future, request))
    except ImageVerificationRuntimeContractError as exc:
        raise ImageVerificationRuntimeContractError(
            exc.code,
            str(exc),
            exc.votes,
            callback_submit_deadline_monotonic=(
                budget.callback_deadline_monotonic
            ),
        ) from exc
    return _current_consensus(votes)


def _run_local_sources(
    credentials: Any,
    request: ImageVerificationRequest,
    policy: ImageVerificationPolicy,
    runtime: ImageVerificationRuntime,
    budget: VerificationBudget,
) -> ImageVerificationDecision:
    model_source = _image_provider_label(credentials)
    futures: dict[str, Future[TimedResult[Any]]] = {}
    votes: dict[str, ImageVerificationVote] = {}
    model_future: Future[ImageVerificationVote] | None = None
    model_reason = ""
    while budget.remaining_seconds() > 0:
        submit_available_ocr(runtime, request, futures, votes)
        harvest_ocr_futures(futures, votes, request, budget)
        decision = _current_consensus(votes)
        if decision is not None:
            return _finalize_decision(
                decision.answer, votes, model_source, model_future,
                model_reason, policy, budget,
            )
        _raise_if_local_votes_unsafe(votes)
        if model_future is None:
            model_reason = _local_model_start_reason(votes, futures, budget)
            if model_reason:
                model_future = _start_model(
                    credentials, request, policy, runtime, budget,
                    model_source, model_reason,
                )
        decision = _harvest_model(model_future, votes, model_source)
        if decision is not None:
            return _finalize_decision(
                decision.answer, votes, model_source, model_future,
                model_reason, policy, budget,
            )
        _raise_if_all_sources_diverge(votes, model_source)
        _wait_for_local_progress(futures, model_future, budget)
    raise ImageVerificationRuntimeContractError(
        "verification_deadline_exceeded",
        "image verification sources exceeded callback deadline",
        _deadline_votes(votes, futures, model_source, model_future),
    )


def _harvest_model(
    model_future: Future[ImageVerificationVote] | None,
    votes: dict[str, ImageVerificationVote],
    model_source: str,
) -> ImageVerificationDecision | None:
    if model_future is None or not model_future.done():
        return None
    votes[model_source] = model_future.result()
    return _current_consensus(votes)


def _raise_if_all_sources_diverge(
    votes: dict[str, ImageVerificationVote],
    model_source: str,
) -> None:
    if all(source in votes for source in (model_source, "rapidocr", "ddddocr")):
        _consensus_decision(_ordered_votes(votes, model_source))


def _current_consensus(
    votes: dict[str, ImageVerificationVote],
) -> ImageVerificationDecision | None:
    from .membership_challenges import _consensus_or_none

    return _consensus_or_none(tuple(votes.values()))


def _consensus_decision(
    votes: tuple[ImageVerificationVote, ...],
) -> ImageVerificationDecision:
    from .membership_challenges import _consensus_decision as decide

    return decide(votes)


def _raise_if_local_votes_unsafe(
    votes: dict[str, ImageVerificationVote],
) -> None:
    local = [
        votes[source]
        for source in ("rapidocr", "ddddocr")
        if source in votes
    ]
    if len(local) < 2 or any(vote.status == "accepted" for vote in local):
        return
    raise ImageVerificationRuntimeContractError(
        "verification_no_safe_local_vote",
        "both local OCR sources completed without a safe vote",
        tuple(local),
    )


def _local_model_start_reason(
    votes: dict[str, ImageVerificationVote],
    futures: dict[str, Future[TimedResult[Any]]],
    budget: VerificationBudget,
) -> str:
    if all(source in votes for source in ("rapidocr", "ddddocr")):
        return "local_divergence"
    if monotonic() >= budget.model_hedge_monotonic:
        return "deadline_hedge"
    if not futures and votes:
        return "local_divergence"
    return ""


def _remote_model_start_reason(
    votes: dict[str, ImageVerificationVote],
    budget: VerificationBudget,
) -> str:
    if votes:
        return "local_divergence"
    if monotonic() >= budget.model_hedge_monotonic:
        return "deadline_hedge"
    return ""


def _start_model(
    credentials: Any,
    request: ImageVerificationRequest,
    policy: ImageVerificationPolicy,
    runtime: ImageVerificationRuntime,
    budget: VerificationBudget,
    model_source: str,
    reason: str,
) -> Future[ImageVerificationVote]:
    if reason == "local_divergence" and (
        budget.remaining_seconds() + (SLOT_POLL_SECONDS * 2)
        < policy.model_tail_budget_seconds
    ):
        raise ImageVerificationRuntimeContractError(
            "verification_model_budget_insufficient",
            "remaining deadline budget is below the calibrated model tail",
        )
    if credentials is None:
        raise ImageVerificationRuntimeContractError(
            "verification_ai_unavailable",
            "no healthy approved multimodal provider",
        )
    return runtime.submit_model(
        lambda: _timed_model_vote(
            model_source,
            credentials,
            request,
            policy,
            budget,
        )
    )


def _timed_model_vote(
    source: str,
    credentials: Any,
    request: ImageVerificationRequest,
    policy: ImageVerificationPolicy,
    budget: VerificationBudget,
) -> ImageVerificationVote:
    from .membership_challenges import _recognition_vote, _solve_search_join_image

    started_at = datetime.now(UTC)
    started = monotonic()
    vote = _recognition_vote(
        source,
        lambda: _solve_search_join_image(
            credentials,
            request.image_bytes,
            request.mime_type,
            request.challenge_text,
            deadline_monotonic=budget.callback_deadline_monotonic,
            timeout=policy.model_timeout_seconds,
            retry_min_budget_seconds=policy.reasoning_retry_min_budget_seconds,
        ),
        MIN_IMAGE_VERIFICATION_CONFIDENCE,
        request,
    )
    completed = monotonic()
    result = TimedResult(
        value=None,
        started_at=started_at,
        completed_at=datetime.now(UTC),
        duration_ms=max(0, int((completed - started) * 1000)),
        completed_monotonic=completed,
    )
    return vote_with_timing(
        vote,
        result,
        late=completed >= budget.callback_deadline_monotonic,
    )


def _wait_for_local_progress(
    futures: dict[str, Future[TimedResult[Any]]],
    model_future: Future[ImageVerificationVote] | None,
    budget: VerificationBudget,
) -> None:
    if any(future.done() for future in futures.values()):
        return
    _wait_for_progress(model_future, budget)


def _wait_for_remote_progress(
    remote_future: Future[RemoteOcrResult],
    model_future: Future[ImageVerificationVote] | None,
    budget: VerificationBudget,
) -> None:
    if remote_future.done():
        return
    _wait_for_progress(model_future, budget)


def _wait_for_progress(
    model_future: Future[ImageVerificationVote] | None,
    budget: VerificationBudget,
) -> None:
    if model_future is not None and model_future.done():
        return
    remaining = budget.remaining_seconds()
    hedge_wait = budget.seconds_until_hedge() if model_future is None else remaining
    wait_seconds = min(SLOT_POLL_SECONDS, remaining, hedge_wait or SLOT_POLL_SECONDS)
    if wait_seconds > 0:
        threading.Event().wait(wait_seconds)


def _finalize_decision(
    answer: str,
    votes: dict[str, ImageVerificationVote],
    model_source: str,
    model_future: Future[ImageVerificationVote] | None,
    model_reason: str,
    policy: ImageVerificationPolicy,
    budget: VerificationBudget,
) -> ImageVerificationDecision:
    selected = dict(votes)
    if model_future is None:
        selected[model_source] = ImageVerificationVote(model_source, "not_started")
    elif model_source not in selected:
        selected[model_source] = (
            model_future.result()
            if model_future.done()
            else ImageVerificationVote(model_source, "running")
        )
    ordered = _ordered_votes(selected, model_source)
    matching = {vote.source for vote in ordered if vote.answer == answer}
    return ImageVerificationDecision(
        answer=answer,
        confidence=IMAGE_VERIFICATION_CONSENSUS_COUNT / IMAGE_VERIFICATION_SOURCE_COUNT,
        votes=ordered,
        model_waited=model_source in votes,
        model_started=model_future is not None,
        model_start_reason=model_reason,
        consensus_source=(
            "local_ocr" if {"rapidocr", "ddddocr"} <= matching else "model_ocr"
        ),
        contract_version=policy.contract_version,
        challenge_observed_at=budget.observed_at.isoformat(),
        model_hedge_at=budget.model_hedge_at.isoformat(),
        callback_submit_deadline=budget.callback_deadline_at.isoformat(),
        callback_submit_deadline_monotonic=budget.callback_deadline_monotonic,
    )


def _deadline_votes(
    votes: dict[str, ImageVerificationVote],
    futures: dict[str, Future[TimedResult[Any]]],
    model_source: str,
    model_future: Future[ImageVerificationVote] | None,
) -> tuple[ImageVerificationVote, ...]:
    selected = dict(votes)
    for source in ("rapidocr", "ddddocr"):
        if source not in selected:
            selected[source] = ImageVerificationVote(
                source,
                "timeout",
                detail="running" if source in futures else "waiting_slot",
                late=True,
            )
    if model_future is None:
        selected[model_source] = ImageVerificationVote(model_source, "not_started")
    elif model_source not in selected:
        selected[model_source] = ImageVerificationVote(
            model_source,
            "timeout",
            detail="running",
            late=True,
        )
    return _ordered_votes(selected, model_source)


def _ordered_votes(
    votes: dict[str, ImageVerificationVote],
    model_source: str,
) -> tuple[ImageVerificationVote, ...]:
    return tuple(
        votes[source]
        for source in (model_source, "rapidocr", "ddddocr")
        if source in votes
    )


def _image_provider_label(credentials: Any) -> str:
    if credentials is None:
        return "multimodal"
    from .membership_challenges import _image_provider_label

    return _image_provider_label(credentials)

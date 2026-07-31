from __future__ import annotations

from concurrent.futures import Future
from typing import Any

from app.integrations.telegram.search_join import (
    ImageVerificationRequest,
    ImageVerificationRuntimeContractError,
    ImageVerificationVote,
)
from app import image_verification_ocr

from .image_verification_client import RemoteOcrError, RemoteOcrResult
from .image_verification_runtime import (
    ImageVerificationRuntime,
    TimedResult,
    VerificationBudget,
)

RAPIDOCR_MIN_CONFIDENCE = 0.50
DDDDOCR_MIN_CONFIDENCE = 0.50


def remote_ocr_votes(
    future: Future[RemoteOcrResult],
    request: ImageVerificationRequest,
) -> dict[str, ImageVerificationVote]:
    try:
        result = future.result()
    except RemoteOcrError as exc:
        raise ImageVerificationRuntimeContractError(
            exc.code,
            str(exc),
        ) from exc
    thresholds = {
        "rapidocr": RAPIDOCR_MIN_CONFIDENCE,
        "ddddocr": DDDDOCR_MIN_CONFIDENCE,
    }
    return {
        source.source: _remote_source_vote(
            source,
            thresholds[source.source],
            request,
        )
        for source in result.sources
        if source.source in thresholds
    }


def _remote_source_vote(
    source: Any,
    threshold: float,
    request: ImageVerificationRequest,
) -> ImageVerificationVote:
    if source.status != "complete":
        return ImageVerificationVote(
            source.source,
            "failed",
            detail=source.detail,
            started_at=source.started_at,
            completed_at=source.completed_at,
            duration_ms=source.duration_ms,
            late=source.late,
        )
    vote = _ocr_vote(
        source.source,
        lambda _image: source.candidates,
        threshold,
        request,
    )
    return ImageVerificationVote(
        source=vote.source,
        status="late" if source.late else vote.status,
        answer=vote.answer,
        confidence=vote.confidence,
        in_candidates=vote.in_candidates,
        detail=vote.detail,
        started_at=source.started_at,
        completed_at=source.completed_at,
        duration_ms=source.duration_ms,
        late=source.late,
    )


def submit_available_ocr(
    runtime: ImageVerificationRuntime,
    request: ImageVerificationRequest,
    futures: dict[str, Future[TimedResult[Any]]],
    votes: dict[str, ImageVerificationVote],
) -> None:
    sources = (
        (
            "rapidocr",
            runtime.rapidocr,
            image_verification_ocr.recognize_rapidocr_variants,
        ),
        (
            "ddddocr",
            runtime.ddddocr,
            image_verification_ocr.recognize_ddddocr_variants,
        ),
    )
    for source, slot, recognize in sources:
        if source in futures or source in votes:
            continue
        future = slot.try_submit(
            lambda operation=recognize: operation(request.image_bytes)
        )
        if future is not None:
            futures[source] = future


def harvest_ocr_futures(
    futures: dict[str, Future[TimedResult[Any]]],
    votes: dict[str, ImageVerificationVote],
    request: ImageVerificationRequest,
    budget: VerificationBudget,
) -> None:
    thresholds = {
        "rapidocr": RAPIDOCR_MIN_CONFIDENCE,
        "ddddocr": DDDDOCR_MIN_CONFIDENCE,
    }
    for source, future in tuple(futures.items()):
        if not future.done():
            continue
        result = future.result()
        votes[source] = _timed_ocr_vote(
            source,
            result,
            thresholds[source],
            request,
            budget,
        )
        futures.pop(source)


def _timed_ocr_vote(
    source: str,
    result: TimedResult[Any],
    threshold: float,
    request: ImageVerificationRequest,
    budget: VerificationBudget,
) -> ImageVerificationVote:
    if result.error_type:
        vote = ImageVerificationVote(
            source,
            "failed",
            detail=result.error_type,
        )
    else:
        vote = _ocr_vote(
            source,
            lambda _image: result.value or (),
            threshold,
            request,
        )
    late = result.completed_monotonic >= budget.callback_deadline_monotonic
    return vote_with_timing(vote, result, late=late)


def vote_with_timing(
    vote: ImageVerificationVote,
    result: TimedResult[Any],
    *,
    late: bool,
) -> ImageVerificationVote:
    return ImageVerificationVote(
        source=vote.source,
        status="late" if late else vote.status,
        answer=vote.answer,
        confidence=vote.confidence,
        in_candidates=vote.in_candidates,
        detail=vote.detail,
        started_at=result.started_at.isoformat(),
        completed_at=result.completed_at.isoformat(),
        duration_ms=result.duration_ms,
        late=late,
    )


def _ocr_vote(
    source: str,
    recognize,
    threshold: float,
    request: ImageVerificationRequest,
) -> ImageVerificationVote:
    from .membership_challenges import _ocr_vote as membership_ocr_vote

    return membership_ocr_vote(source, recognize, threshold, request)

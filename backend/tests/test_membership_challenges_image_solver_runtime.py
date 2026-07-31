from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import sleep
from types import SimpleNamespace

import pytest

from app.integrations.telegram import search_join
from app.services import membership_challenges
from app.services.image_verification_client import RemoteOcrResult, RemoteOcrSource
from app.services.image_verification_runtime import ImageVerificationRuntime
from membership_challenges_image_solver_test_support import (
    _configure_contract,
    _install_sources,
    _request,
)


@pytest.fixture(autouse=True)
def _configured_verification_contract(monkeypatch):
    _configure_contract(monkeypatch)


@pytest.mark.no_postgres
def test_running_model_remains_registered_after_local_consensus(monkeypatch) -> None:
    _install_sources(monkeypatch, ddddocr_answer="10", rapidocr_answer="10")
    release_model = Event()
    rapid_release = Event()
    runtime = ImageVerificationRuntime(model_concurrency=1)

    def slow_model(*_args, **_kwargs):
        release_model.wait(timeout=1)
        return SimpleNamespace(answer="9", confidence=0.95)

    def slow_rapid(_image):
        rapid_release.wait(timeout=1)
        return (("10", 0.9),)

    monkeypatch.setattr(membership_challenges.ai_gateway, "solve_image_verification", slow_model)
    monkeypatch.setattr(
        membership_challenges.image_verification_ocr,
        "recognize_rapidocr_variants",
        slow_rapid,
    )
    solver = membership_challenges.build_search_join_image_verification_solver(object(), runtime=runtime)

    with ThreadPoolExecutor(max_workers=1) as executor:
        solver_future = executor.submit(solver, _request(["9", "10"]))
        sleep(0.55)
        rapid_release.set()
        decision = solver_future.result(timeout=1)

    assert decision.answer == "10"
    assert decision.model_started is True
    assert runtime.registry.count() == 1
    release_model.set()
    assert runtime.registry.wait_empty() is True


@pytest.mark.no_postgres
def test_uncalibrated_deadline_fails_closed(monkeypatch) -> None:
    _install_sources(monkeypatch, ddddocr_answer="10", rapidocr_answer="10")
    policy = membership_challenges.ImageVerificationPolicy(
        enabled=True,
        contract_version="test-v1",
        callback_acceptance_seconds=0,
        callback_headroom_seconds=0,
        model_tail_budget_seconds=0,
        model_timeout_seconds=1,
        reasoning_retry_min_budget_seconds=0.1,
        model_concurrency=1,
    )
    solver = membership_challenges.build_search_join_image_verification_solver(object(), policy=policy)

    with pytest.raises(search_join.ImageVerificationRuntimeContractError) as raised:
        solver(_request(["9", "10"]))

    assert raised.value.code == "verification_deadline_not_calibrated"


def _remote_result(*, rapid_answer: str, dddd_answer: str, duration_ms: int) -> RemoteOcrResult:
    return RemoteOcrResult(
        request_id="a" * 64,
        input_hash="d" * 64,
        worker_generation="generation-1",
        sources=tuple(
            RemoteOcrSource(
                source=source,
                status="complete",
                candidates=((answer, 0.9),),
                started_at="start",
                completed_at="end",
                duration_ms=duration_ms,
                late=False,
                detail="",
            )
            for source, answer in (("rapidocr", rapid_answer), ("ddddocr", dddd_answer))
        ),
    )


def _remote_policy(*, acceptance: float = 1.0) -> membership_challenges.ImageVerificationPolicy:
    return membership_challenges.ImageVerificationPolicy(
        enabled=True,
        contract_version="test-v1",
        callback_acceptance_seconds=acceptance,
        callback_headroom_seconds=0.05 if acceptance < 1 else 0.1,
        model_tail_budget_seconds=0.20 if acceptance < 1 else 0.4,
        model_timeout_seconds=0.20 if acceptance < 1 else 0.4,
        reasoning_retry_min_budget_seconds=0.05 if acceptance < 1 else 0.1,
        model_concurrency=1,
        ocr_backend="remote",
        worker_url="http://worker:8091",
        worker_token="token",
    )


def _remote_request() -> search_join.ImageVerificationRequest:
    return search_join.ImageVerificationRequest(
        image_bytes=b"image",
        mime_type="image/png",
        candidate_answers=("9", "10"),
        challenge_text="数学题",
        challenge_fingerprint_hash="b" * 64,
        candidate_hash="c" * 64,
    )


@pytest.mark.no_postgres
def test_remote_ocr_path_never_calls_dispatcher_native_ocr(monkeypatch) -> None:
    model_calls = _install_sources(monkeypatch)

    def native_fallback(*_args, **_kwargs):
        raise AssertionError("remote mode must not load local OCR")

    monkeypatch.setattr(membership_challenges.image_verification_ocr, "recognize_rapidocr_variants", native_fallback)
    monkeypatch.setattr(membership_challenges.image_verification_ocr, "recognize_ddddocr_variants", native_fallback)
    monkeypatch.setattr(
        "app.services.search_join_image_solver.ImageVerificationWorkerClient.recognize",
        lambda *_args, **_kwargs: _remote_result(rapid_answer="10", dddd_answer="10", duration_ms=10),
    )
    solver = membership_challenges.build_search_join_image_verification_solver(
        object(),
        action_id="action-1",
        policy=_remote_policy(),
        runtime=ImageVerificationRuntime(model_concurrency=1),
    )

    decision = solver(_remote_request())

    assert decision.answer == "10"
    assert decision.consensus_source == "local_ocr"
    assert model_calls == []


@pytest.mark.no_postgres
def test_remote_ocr_is_harvested_after_hedged_model_finishes_first(monkeypatch) -> None:
    model_calls = _install_sources(monkeypatch, model_answer="10")

    def delayed_remote(*_args, **_kwargs):
        sleep(0.08)
        return _remote_result(rapid_answer="9", dddd_answer="10", duration_ms=80)

    monkeypatch.setattr(
        "app.services.search_join_image_solver.ImageVerificationWorkerClient.recognize",
        delayed_remote,
    )
    solver = membership_challenges.build_search_join_image_verification_solver(
        object(),
        action_id="action-1",
        policy=_remote_policy(acceptance=0.30),
        runtime=ImageVerificationRuntime(model_concurrency=1),
    )

    decision = solver(_remote_request())

    assert decision.answer == "10"
    assert decision.consensus_source == "model_ocr"
    assert model_calls == [1]

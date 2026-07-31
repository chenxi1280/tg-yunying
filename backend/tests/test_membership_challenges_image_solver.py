from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from time import monotonic, sleep

import pytest

from app.integrations.telegram import search_join
from app.services import membership_challenges
from app.services.image_verification_runtime import (
    ImageVerificationRuntime,
)
from app.services.image_verification_client import (
    RemoteOcrResult,
    RemoteOcrSource,
)


@pytest.fixture(autouse=True)
def _configured_verification_contract(monkeypatch):
    settings = SimpleNamespace(
        image_verification_contract_enabled=True,
        image_verification_contract_version="test-v1",
        image_verification_callback_acceptance_seconds=1.0,
        image_verification_callback_headroom_seconds=0.1,
        image_verification_model_tail_budget_seconds=0.4,
        image_verification_model_timeout_seconds=0.4,
        image_verification_reasoning_retry_min_budget_seconds=0.1,
        image_verification_model_concurrency=2,
        image_verification_ocr_backend="local",
        image_verification_worker_url="",
        image_verification_worker_token="",
    )
    monkeypatch.setattr(
        membership_challenges,
        "get_settings",
        lambda: settings,
    )


def _request(
    candidates: list[str],
    *,
    challenge_text: str = "人机验证 请计算结果",
) -> search_join.ImageVerificationRequest:
    return search_join.ImageVerificationRequest(
        image_bytes=b"image",
        mime_type="image/png",
        candidate_answers=tuple(candidates),
        challenge_text=challenge_text,
    )


def _install_sources(
    monkeypatch,
    *,
    model_answer: str = "10",
    model_confidence: float = 0.95,
    ddddocr_answer: str = "10",
    rapidocr_answer: str = "9",
) -> list[int]:
    provider = SimpleNamespace(
        id=1,
        provider_name="MiMo",
        model_name="mimo-v2.5",
    )
    calls: list[int] = []
    monkeypatch.setattr(
        membership_challenges,
        "_image_verification_providers",
        lambda _session: [provider],
    )
    monkeypatch.setattr(
        membership_challenges,
        "ai_provider_credentials",
        lambda value: value,
    )

    def solve(selected, *_args, **_kwargs):
        calls.append(selected.id)
        return SimpleNamespace(
            answer=model_answer,
            confidence=model_confidence,
        )

    monkeypatch.setattr(
        membership_challenges.ai_gateway,
        "solve_image_verification",
        solve,
    )
    monkeypatch.setattr(
        membership_challenges.image_verification_ocr,
        "recognize_ddddocr_variants",
        lambda _image: ((ddddocr_answer, 0.81),),
    )
    monkeypatch.setattr(
        membership_challenges.image_verification_ocr,
        "recognize_rapidocr_variants",
        lambda _image: ((rapidocr_answer, 0.88),),
    )
    return calls


@pytest.mark.no_postgres
def test_image_solver_accepts_model_and_ddddocr_two_of_three(
    monkeypatch,
) -> None:
    calls = _install_sources(monkeypatch)
    solver = membership_challenges.build_search_join_image_verification_solver(
        object(),
    )

    decision = solver(_request(["8", "9", "10", "11"]))

    assert decision.answer == "10"
    assert decision.confidence == pytest.approx(2 / 3)
    assert [vote.status for vote in decision.votes] == [
        "accepted",
        "accepted",
        "accepted",
    ]
    assert calls == [1]


@pytest.mark.no_postgres
def test_image_solver_accepts_two_ocr_votes_without_waiting_model(
    monkeypatch,
) -> None:
    _install_sources(
        monkeypatch,
        ddddocr_answer="10",
        rapidocr_answer="10",
    )
    monkeypatch.setattr(
        membership_challenges.ai_gateway,
        "solve_image_verification",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("AI HTTP 503"),
        ),
    )
    solver = membership_challenges.build_search_join_image_verification_solver(
        object(),
    )

    decision = solver(_request(["9", "10"]))

    assert decision.answer == "10"
    assert decision.model_waited is False
    assert decision.votes[0].status == "not_started"
    assert [vote.source for vote in decision.votes[1:]] == [
        "rapidocr",
        "ddddocr",
    ]


@pytest.mark.no_postgres
def test_image_solver_requires_consensus_instead_of_guessing(
    monkeypatch,
) -> None:
    _install_sources(
        monkeypatch,
        model_answer="8",
        ddddocr_answer="9",
        rapidocr_answer="10",
    )
    solver = membership_challenges.build_search_join_image_verification_solver(
        object(),
    )

    with pytest.raises(
        search_join.ImageVerificationConsensusUnavailableError,
    ) as raised:
        solver(_request(["8", "9", "10"]))

    assert [vote.answer for vote in raised.value.votes] == ["8", "10", "9"]


@pytest.mark.no_postgres
def test_image_solver_rejects_two_matching_answers_outside_candidates(
    monkeypatch,
) -> None:
    _install_sources(
        monkeypatch,
        model_answer="99",
        ddddocr_answer="99",
        rapidocr_answer="10",
    )
    solver = membership_challenges.build_search_join_image_verification_solver(
        object(),
    )

    with pytest.raises(
        search_join.ImageVerificationConsensusUnavailableError,
    ) as raised:
        solver(_request(["8", "9", "10"]))

    assert raised.value.votes[0].status == "unsafe"
    assert raised.value.votes[2].status == "unsafe"


@pytest.mark.no_postgres
def test_image_solver_calculates_ocr_expression_before_consensus(
    monkeypatch,
) -> None:
    _install_sources(
        monkeypatch,
        model_answer="19",
        ddddocr_answer="12 + 7 = ?",
        rapidocr_answer="12+7",
    )
    solver = membership_challenges.build_search_join_image_verification_solver(
        object(),
    )

    decision = solver(_request(["17", "18", "19"]))

    assert decision.answer == "19"
    assert [vote.answer for vote in decision.votes] == ["", "19", "19"]
    assert decision.model_waited is False


@pytest.mark.no_postgres
def test_malformed_ocr_expression_does_not_vote_for_first_number() -> None:
    assert membership_challenges._normalize_math_answer("12+/=?") == ""


@pytest.mark.no_postgres
def test_image_solver_preserves_exact_string_prompt_and_value(
    monkeypatch,
) -> None:
    prompts: list[str] = []
    _install_sources(
        monkeypatch,
        model_answer="A7B9",
        ddddocr_answer="A7 B9",
        rapidocr_answer="C3D1",
    )

    def solve(_provider, *_args, **kwargs):
        prompts.append(kwargs["prompt"])
        return SimpleNamespace(answer="A7B9", confidence=0.95)

    monkeypatch.setattr(
        membership_challenges.ai_gateway,
        "solve_image_verification",
        solve,
    )
    solver = membership_challenges.build_search_join_image_verification_solver(
        object(),
    )

    decision = solver(
        _request(
            ["A7B9", "C3D1"],
            challenge_text="人机验证 请选择图片中的字符串",
        )
    )

    assert decision.answer == "A7B9"
    assert len(prompts) == 1
    assert "这是是一段数字+字符的字符串你来告诉我结果" in prompts[0]


@pytest.mark.no_postgres
def test_image_solver_uses_exact_math_prompt_without_candidates(
    monkeypatch,
) -> None:
    prompts: list[str] = []
    _install_sources(
        monkeypatch,
        model_answer="0",
        ddddocr_answer="0",
        rapidocr_answer="23",
    )

    def solve(_provider, *_args, **kwargs):
        prompts.append(kwargs["prompt"])
        return SimpleNamespace(answer="0", confidence=0.95)

    monkeypatch.setattr(
        membership_challenges.ai_gateway,
        "solve_image_verification",
        solve,
    )
    solver = membership_challenges.build_search_join_image_verification_solver(
        object(),
    )

    assert solver(_request(["23", "105", "0"])).answer == "0"
    assert len(prompts) == 1
    assert "这是数学题，都是全数字，你来给出答案" in prompts[0]
    assert '["23", "105", "0"]' not in prompts[0]


@pytest.mark.no_postgres
def test_image_solver_uses_only_first_healthy_model(
    monkeypatch,
) -> None:
    providers = [
        SimpleNamespace(id=5, provider_name="MiniMax", model_name="MiniMax-M3"),
        SimpleNamespace(id=1, provider_name="MiMo", model_name="mimo-v2.5"),
    ]
    calls: list[int] = []
    monkeypatch.setattr(
        membership_challenges,
        "_image_verification_providers",
        lambda _session: providers,
    )
    monkeypatch.setattr(
        membership_challenges,
        "ai_provider_credentials",
        lambda value: value,
    )

    def solve(provider, *_args, **_kwargs):
        calls.append(provider.id)
        return SimpleNamespace(answer="10", confidence=0.95)

    monkeypatch.setattr(
        membership_challenges.ai_gateway,
        "solve_image_verification",
        solve,
    )
    monkeypatch.setattr(
        membership_challenges.image_verification_ocr,
        "recognize_ddddocr_variants",
        lambda _image: (("10", 0.8),),
    )
    monkeypatch.setattr(
        membership_challenges.image_verification_ocr,
        "recognize_rapidocr_variants",
        lambda _image: (("9", 0.8),),
    )
    solver = membership_challenges.build_search_join_image_verification_solver(
        object(),
    )

    assert solver(_request(["9", "10"])).answer == "10"
    assert calls == [1]


@pytest.mark.no_postgres
def test_image_solver_starts_only_two_local_sources_in_parallel(
    monkeypatch,
) -> None:
    _install_sources(monkeypatch)
    barrier = Barrier(2, timeout=1)
    model_calls = 0

    def model(*_args, **_kwargs):
        nonlocal model_calls
        model_calls += 1
        return SimpleNamespace(answer="10", confidence=0.95)

    def ddddocr(_image):
        barrier.wait()
        return (("10", 0.80),)

    def rapidocr(_image):
        barrier.wait()
        return (("9", 0.80),)

    monkeypatch.setattr(
        membership_challenges.ai_gateway,
        "solve_image_verification",
        model,
    )
    monkeypatch.setattr(
        membership_challenges.image_verification_ocr,
        "recognize_ddddocr_variants",
        ddddocr,
    )
    monkeypatch.setattr(
        membership_challenges.image_verification_ocr,
        "recognize_rapidocr_variants",
        rapidocr,
    )
    solver = membership_challenges.build_search_join_image_verification_solver(
        object(),
    )

    assert solver(_request(["9", "10"])).answer == "10"
    assert model_calls == 1


@pytest.mark.no_postgres
def test_two_ocr_consensus_does_not_wait_for_model(monkeypatch) -> None:
    _install_sources(
        monkeypatch,
        ddddocr_answer="10",
        rapidocr_answer="10",
    )
    release_model = Event()

    def slow_model(*_args, **_kwargs):
        release_model.wait(timeout=1)
        return SimpleNamespace(answer="9", confidence=0.95)

    monkeypatch.setattr(
        membership_challenges.ai_gateway,
        "solve_image_verification",
        slow_model,
    )
    solver = membership_challenges.build_search_join_image_verification_solver(
        object(),
    )

    started = monotonic()
    decision = solver(_request(["9", "10"]))
    elapsed = monotonic() - started
    release_model.set()

    assert decision.answer == "10"
    assert decision.model_waited is False
    assert decision.votes[0].status == "not_started"
    assert elapsed < 0.5


@pytest.mark.no_postgres
def test_one_ocr_engine_variants_still_form_only_one_vote() -> None:
    request = _request(["9", "10"])

    vote = membership_challenges._ocr_vote(
        "rapidocr",
        lambda _image: (("10", 0.9), ("9", 0.9)),
        0.5,
        request,
    )

    assert vote.status == "unsafe"
    assert vote.answer == ""


@pytest.mark.no_postgres
def test_rapidocr_inference_is_serialized_per_worker(monkeypatch) -> None:
    active = 0
    maximum = 0

    def recognition(_image):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        sleep(0.02)
        active -= 1
        return "10", 0.9

    monkeypatch.setattr(
        membership_challenges.image_verification_ocr,
        "_recognize_with_rapidocr",
        recognition,
    )
    recognize = (
        membership_challenges.image_verification_ocr
        .recognize_with_rapidocr
    )

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = tuple(executor.map(recognize, [b"image"] * 4))

    assert results == (("10", 0.9),) * 4
    assert maximum == 1


@pytest.mark.no_postgres
def test_running_model_remains_registered_after_local_consensus(
    monkeypatch,
) -> None:
    _install_sources(
        monkeypatch,
        ddddocr_answer="10",
        rapidocr_answer="10",
    )
    release_model = Event()
    rapid_release = Event()
    runtime = ImageVerificationRuntime(model_concurrency=1)

    def slow_model(*_args, **_kwargs):
        release_model.wait(timeout=1)
        return SimpleNamespace(answer="9", confidence=0.95)

    def slow_rapid(_image):
        rapid_release.wait(timeout=1)
        return (("10", 0.9),)

    monkeypatch.setattr(
        membership_challenges.ai_gateway,
        "solve_image_verification",
        slow_model,
    )
    monkeypatch.setattr(
        membership_challenges.image_verification_ocr,
        "recognize_rapidocr_variants",
        slow_rapid,
    )
    solver = membership_challenges.build_search_join_image_verification_solver(
        object(),
        runtime=runtime,
    )
    decision_box = []

    with ThreadPoolExecutor(max_workers=1) as executor:
        solver_future = executor.submit(solver, _request(["9", "10"]))
        sleep(0.55)
        rapid_release.set()
        decision_box.append(solver_future.result(timeout=1))

    assert decision_box[0].answer == "10"
    assert decision_box[0].model_started is True
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
    solver = membership_challenges.build_search_join_image_verification_solver(
        object(),
        policy=policy,
    )

    with pytest.raises(search_join.ImageVerificationRuntimeContractError) as raised:
        solver(_request(["9", "10"]))

    assert raised.value.code == "verification_deadline_not_calibrated"


@pytest.mark.no_postgres
def test_remote_ocr_path_never_calls_dispatcher_native_ocr(
    monkeypatch,
) -> None:
    model_calls = _install_sources(monkeypatch)

    def native_fallback(*_args, **_kwargs):
        raise AssertionError("remote mode must not load local OCR")

    monkeypatch.setattr(
        membership_challenges.image_verification_ocr,
        "recognize_rapidocr_variants",
        native_fallback,
    )
    monkeypatch.setattr(
        membership_challenges.image_verification_ocr,
        "recognize_ddddocr_variants",
        native_fallback,
    )
    remote_result = RemoteOcrResult(
        request_id="a" * 64,
        input_hash="d" * 64,
        worker_generation="generation-1",
        sources=tuple(
            RemoteOcrSource(
                source=source,
                status="complete",
                candidates=(("10", 0.9),),
                started_at="start",
                completed_at="end",
                duration_ms=10,
                late=False,
                detail="",
            )
            for source in ("rapidocr", "ddddocr")
        ),
    )
    monkeypatch.setattr(
        "app.services.search_join_image_solver.ImageVerificationWorkerClient.recognize",
        lambda *_args, **_kwargs: remote_result,
    )
    policy = membership_challenges.ImageVerificationPolicy(
        enabled=True,
        contract_version="test-v1",
        callback_acceptance_seconds=1,
        callback_headroom_seconds=0.1,
        model_tail_budget_seconds=0.4,
        model_timeout_seconds=0.4,
        reasoning_retry_min_budget_seconds=0.1,
        model_concurrency=1,
        ocr_backend="remote",
        worker_url="http://worker:8091",
        worker_token="token",
    )
    solver = membership_challenges.build_search_join_image_verification_solver(
        object(),
        action_id="action-1",
        policy=policy,
        runtime=ImageVerificationRuntime(model_concurrency=1),
    )
    request = search_join.ImageVerificationRequest(
        image_bytes=b"image",
        mime_type="image/png",
        candidate_answers=("9", "10"),
        challenge_text="数学题",
        challenge_fingerprint_hash="b" * 64,
        candidate_hash="c" * 64,
    )

    decision = solver(request)

    assert decision.answer == "10"
    assert decision.consensus_source == "local_ocr"
    assert model_calls == []


@pytest.mark.no_postgres
def test_remote_ocr_is_harvested_after_hedged_model_finishes_first(
    monkeypatch,
) -> None:
    model_calls = _install_sources(monkeypatch, model_answer="10")
    remote_result = RemoteOcrResult(
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
                duration_ms=80,
                late=False,
                detail="",
            )
            for source, answer in (
                ("rapidocr", "9"),
                ("ddddocr", "10"),
            )
        ),
    )

    def delayed_remote(*_args, **_kwargs):
        sleep(0.08)
        return remote_result

    monkeypatch.setattr(
        "app.services.search_join_image_solver.ImageVerificationWorkerClient.recognize",
        delayed_remote,
    )
    policy = membership_challenges.ImageVerificationPolicy(
        enabled=True,
        contract_version="test-v1",
        callback_acceptance_seconds=0.30,
        callback_headroom_seconds=0.05,
        model_tail_budget_seconds=0.20,
        model_timeout_seconds=0.20,
        reasoning_retry_min_budget_seconds=0.05,
        model_concurrency=1,
        ocr_backend="remote",
        worker_url="http://worker:8091",
        worker_token="token",
    )
    solver = membership_challenges.build_search_join_image_verification_solver(
        object(),
        action_id="action-1",
        policy=policy,
        runtime=ImageVerificationRuntime(model_concurrency=1),
    )
    request = search_join.ImageVerificationRequest(
        image_bytes=b"image",
        mime_type="image/png",
        candidate_answers=("9", "10"),
        challenge_text="数学题",
        challenge_fingerprint_hash="b" * 64,
        candidate_hash="c" * 64,
    )

    decision = solver(request)

    assert decision.answer == "10"
    assert decision.consensus_source == "model_ocr"
    assert model_calls == [1]

from types import SimpleNamespace
from threading import Barrier

import pytest

from app.integrations.telegram import search_join
from app.services import membership_challenges


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
    tesseract_answer: str = "10",
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
        "recognize_with_tesseract",
        lambda _image: (tesseract_answer, 0.81),
    )
    monkeypatch.setattr(
        membership_challenges.image_verification_ocr,
        "recognize_with_rapidocr",
        lambda _image: (rapidocr_answer, 0.88),
    )
    return calls


@pytest.mark.no_postgres
def test_image_solver_accepts_model_and_tesseract_two_of_three(
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
def test_image_solver_accepts_two_ocr_votes_when_model_is_unavailable(
    monkeypatch,
) -> None:
    _install_sources(
        monkeypatch,
        tesseract_answer="10",
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
    assert decision.votes[0].status == "unavailable"
    assert [vote.source for vote in decision.votes[1:]] == [
        "tesseract",
        "rapidocr",
    ]


@pytest.mark.no_postgres
def test_image_solver_requires_consensus_instead_of_guessing(
    monkeypatch,
) -> None:
    _install_sources(
        monkeypatch,
        model_answer="8",
        tesseract_answer="9",
        rapidocr_answer="10",
    )
    solver = membership_challenges.build_search_join_image_verification_solver(
        object(),
    )

    with pytest.raises(
        search_join.ImageVerificationConsensusUnavailableError,
    ) as raised:
        solver(_request(["8", "9", "10"]))

    assert [vote.answer for vote in raised.value.votes] == ["8", "9", "10"]


@pytest.mark.no_postgres
def test_image_solver_rejects_two_matching_answers_outside_candidates(
    monkeypatch,
) -> None:
    _install_sources(
        monkeypatch,
        model_answer="99",
        tesseract_answer="99",
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
    assert raised.value.votes[1].status == "unsafe"


@pytest.mark.no_postgres
def test_image_solver_calculates_ocr_expression_before_consensus(
    monkeypatch,
) -> None:
    _install_sources(
        monkeypatch,
        model_answer="19",
        tesseract_answer="12 + 7 = ?",
        rapidocr_answer="12+7",
    )
    solver = membership_challenges.build_search_join_image_verification_solver(
        object(),
    )

    decision = solver(_request(["17", "18", "19"]))

    assert decision.answer == "19"
    assert [vote.answer for vote in decision.votes] == ["19", "19", "19"]


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
        tesseract_answer="A7 B9",
        rapidocr_answer="A7B9",
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
    _install_sources(monkeypatch, model_answer="0", tesseract_answer="0")

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
        "recognize_with_tesseract",
        lambda _image: ("10", 0.8),
    )
    monkeypatch.setattr(
        membership_challenges.image_verification_ocr,
        "recognize_with_rapidocr",
        lambda _image: ("9", 0.8),
    )
    solver = membership_challenges.build_search_join_image_verification_solver(
        object(),
    )

    assert solver(_request(["9", "10"])).answer == "10"
    assert calls == [1]


@pytest.mark.no_postgres
def test_image_solver_starts_three_sources_in_parallel(monkeypatch) -> None:
    _install_sources(monkeypatch)
    barrier = Barrier(3, timeout=1)

    def model(*_args, **_kwargs):
        barrier.wait()
        return SimpleNamespace(answer="10", confidence=0.95)

    def tesseract(_image):
        barrier.wait()
        return "10", 0.80

    def rapidocr(_image):
        barrier.wait()
        return "9", 0.80

    monkeypatch.setattr(
        membership_challenges.ai_gateway,
        "solve_image_verification",
        model,
    )
    monkeypatch.setattr(
        membership_challenges.image_verification_ocr,
        "recognize_with_tesseract",
        tesseract,
    )
    monkeypatch.setattr(
        membership_challenges.image_verification_ocr,
        "recognize_with_rapidocr",
        rapidocr,
    )
    solver = membership_challenges.build_search_join_image_verification_solver(
        object(),
    )

    assert solver(_request(["9", "10"])).answer == "10"

from __future__ import annotations

from types import SimpleNamespace

from app.integrations.telegram import search_join
from app.services import membership_challenges


def _configure_contract(monkeypatch) -> None:
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
    monkeypatch.setattr(membership_challenges, "get_settings", lambda: settings)


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
    provider = SimpleNamespace(id=1, provider_name="MiMo", model_name="mimo-v2.5")
    calls: list[int] = []
    monkeypatch.setattr(
        membership_challenges,
        "_image_verification_providers",
        lambda _session: [provider],
    )
    monkeypatch.setattr(membership_challenges, "ai_provider_credentials", lambda value: value)

    def solve(selected, *_args, **_kwargs):
        calls.append(selected.id)
        return SimpleNamespace(answer=model_answer, confidence=model_confidence)

    monkeypatch.setattr(membership_challenges.ai_gateway, "solve_image_verification", solve)
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

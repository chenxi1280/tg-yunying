from types import SimpleNamespace

import pytest

from app.services import membership_challenges


@pytest.mark.no_postgres
def test_search_join_image_solver_uses_next_provider_until_candidate_is_safe(monkeypatch) -> None:
    providers = [
        SimpleNamespace(id=1, provider_name="MiMo"),
        SimpleNamespace(id=2, provider_name="MiniMax"),
        SimpleNamespace(id=3, provider_name="MiMo Backup"),
        SimpleNamespace(id=4, provider_name="MiniMax Backup"),
    ]
    results = [
        SimpleNamespace(answer="36", confidence=0.95),
        SimpleNamespace(answer="9", confidence=0.45),
        RuntimeError("provider unavailable"),
        SimpleNamespace(answer="10", confidence=0.92),
    ]
    calls: list[int] = []

    monkeypatch.setattr(
        membership_challenges,
        "_image_verification_providers",
        lambda _session: providers,
    )
    monkeypatch.setattr(membership_challenges, "ai_provider_credentials", lambda provider: provider)

    def solve(provider, *_args, **kwargs):
        calls.append(provider.id)
        result = results[provider.id - 1]
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(membership_challenges.ai_gateway, "solve_image_verification", solve)

    solver = membership_challenges.build_search_join_image_verification_solver(object())

    assert solver is not None
    assert solver(b"image", "image/png", ["8", "9", "10", "11"]) == ("10", 0.92)
    assert calls == [1, 2, 3, 4]

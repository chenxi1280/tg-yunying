from __future__ import annotations

import pytest

from app.services.task_center.account_voice_profile_quality import generate_diverse_voice_profile_batch


pytestmark = pytest.mark.no_postgres


def test_generate_diverse_voice_profile_batch_exposes_generator_error_once():
    calls = 0

    def generator(_account_ids: list[int]) -> list[dict]:
        nonlocal calls
        calls += 1
        raise RuntimeError("AI 面具输出不是完整 JSON")

    with pytest.raises(RuntimeError, match="不是完整 JSON"):
        generate_diverse_voice_profile_batch(generator, [101, 102])

    assert calls == 1

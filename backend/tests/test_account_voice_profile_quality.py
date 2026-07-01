from __future__ import annotations

import pytest

from app.services.task_center.account_voice_profile_quality import generate_diverse_voice_profile_batch


pytestmark = pytest.mark.no_postgres


def _generated_profile(account_id: int, summary: str) -> dict:
    return {
        "account_id": account_id,
        "age_band": "青年" if account_id % 2 else "中年",
        "sentence_length": "短句" if account_id % 2 else "中句",
        "tone_strength": "轻松" if account_id % 2 else "谨慎",
        "emoji_policy": "少用" if account_id % 2 else "不用表情",
        "short_prompt_summary": summary,
    }


def test_generate_diverse_voice_profile_batch_retries_recoverable_generator_error():
    calls = 0

    def generator(account_ids: list[int]) -> list[dict]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("AI 面具输出不是完整 JSON")
        return [
            _generated_profile(account_ids[0], "青年短句，先问位置再接半句"),
            _generated_profile(account_ids[1], "中年中句，先讲体验再轻吐槽"),
        ]

    profiles, scores = generate_diverse_voice_profile_batch(generator, [101, 102])

    assert calls == 2
    assert set(profiles) == {101, 102}
    assert scores[101] > 0

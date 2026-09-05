from __future__ import annotations

import gc
import weakref
from difflib import SequenceMatcher
from types import SimpleNamespace

import pytest

from app.services.task_center import ai_message_memory, ai_message_memory_text


pytestmark = pytest.mark.no_postgres


def test_completed_comparisons_do_not_retain_historical_character_profiles(monkeypatch):
    profile_type = ai_message_memory_text._CharProfile
    references = []

    def record_profile(*args):
        profile = profile_type(*args)
        references.append(weakref.ref(profile))
        return profile

    monkeypatch.setattr(ai_message_memory_text, "_CharProfile", record_profile)
    for index in range(200):
        ai_message_memory_text.text_similarity_reaches(
            "候选内容的局部比较归属", f"不同历史的局部比较归属编号{index}", 0.9,
        )
    gc.collect()

    assert references
    assert all(reference() is None for reference in references)


def test_history_scan_prepares_candidate_once_and_visits_all_rows(monkeypatch):
    original = ai_message_memory_text._char_profile
    observed = []

    def record_profile(value):
        observed.append(value)
        return original(value)

    monkeypatch.setattr(ai_message_memory_text, "_char_profile", record_profile)
    candidate = "公园散步集合地点"
    rows = [SimpleNamespace(normalized_text=f"软件版本故障修复{index}", raw_text="")
            for index in range(120)]

    assert ai_message_memory._first_similar_memory(rows, candidate, 0.9) is None
    assert observed.count(candidate) == 1
    assert observed[1:] == [row.normalized_text for row in rows]


@pytest.mark.parametrize("threshold", [0.0, 0.5, 0.78, 0.8, 1.0, 1.01])
@pytest.mark.parametrize(("left", "right"), [
    ("", ""), ("", "非空文本"), ("重复文本", "重复文本"),
    ("abcabcabd", "abdabcabc"), ("公园散步集合地点", "集合地点就在公园"),
    ("天气很好欢迎报名", "软件故障正在修复"), ("aaabbbc", "abcccc"),
])
def test_similarity_decisions_preserve_the_original_mathematical_contract(
    left, right, threshold,
):
    expected = _reference_score(left, right) >= threshold
    assert ai_message_memory_text.text_similarity_reaches(left, right, threshold) is expected


def _reference_score(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    left_chars, right_chars = set(left), set(right)
    jaccard = len(left_chars & right_chars) / len(left_chars | right_chars)
    return max(jaccard, SequenceMatcher(None, left, right).ratio())


def test_history_scan_keeps_first_match_and_raw_text_normalization():
    rows = [
        SimpleNamespace(normalized_text="软件更新故障", raw_text=""),
        SimpleNamespace(normalized_text="", raw_text=" 公园 集合地点！！ "),
        SimpleNamespace(normalized_text="公园集合地点", raw_text=""),
    ]
    assert ai_message_memory._first_similar_memory(rows, "公园集合地点", 1.0) is rows[1]

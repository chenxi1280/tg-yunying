from __future__ import annotations

import pytest

from app.services.task_center import ai_message_memory_text


pytestmark = pytest.mark.no_postgres


@pytest.mark.parametrize(
    ("left", "right", "threshold"),
    [
        ("天津今天新上榜的老师怎么样", "天津今天新上榜的老师怎么样", 0.80),
        ("天津今天新上榜的老师怎么样", "天津新上榜老师感觉怎么样", 0.78),
        ("石家庄老师价格服务如何", "郑州周末一起去公园放风筝", 0.78),
        ("abcdef", "abcxyz", 0.50),
        ("", "非空", 0.78),
    ],
)
def test_threshold_predicate_preserves_existing_similarity_result(
    left: str,
    right: str,
    threshold: float,
) -> None:
    predicate = getattr(ai_message_memory_text, "text_similarity_reaches", None)
    assert predicate is not None
    expected = ai_message_memory_text.text_similarity(left, right) >= threshold

    assert predicate(left, right, threshold) is expected


def test_low_overlap_pair_skips_sequence_matcher(monkeypatch: pytest.MonkeyPatch) -> None:
    predicate = getattr(ai_message_memory_text, "text_similarity_reaches", None)
    assert predicate is not None

    def unexpected_sequence_matcher(*_args, **_kwargs):
        raise AssertionError("low-overlap history must not invoke SequenceMatcher")

    monkeypatch.setattr(ai_message_memory_text, "SequenceMatcher", unexpected_sequence_matcher)

    assert predicate("石家庄老师价格服务如何", "郑州周末一起去公园放风筝", 0.78) is False

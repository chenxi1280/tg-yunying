from __future__ import annotations

from collections import Counter

import pytest

from app.services.task_center.channel_comment_style_assignment import (
    frozen_comment_style,
    length_matches_style,
)
from app.services.task_center.comment_generation_quality import _style_error


pytestmark = pytest.mark.no_postgres


def test_ten_slots_freeze_two_six_two_length_distribution() -> None:
    styles = [frozen_comment_style("snapshot-a", ordinal) for ordinal in range(1, 11)]

    assert Counter(item.length_tier for item in styles) == {
        "ultra_short": 2,
        "medium": 6,
        "long": 2,
    }
    assert styles == [frozen_comment_style("snapshot-a", ordinal) for ordinal in range(1, 11)]


def test_length_tier_uses_non_whitespace_character_count() -> None:
    style = frozen_comment_style("snapshot-a", 1)
    valid = "真顶" if style.length_tier == "ultra_short" else "围绕这个细节再问一句看看"

    assert length_matches_style(valid, style) is (
        style.minimum_length <= len("".join(valid.split())) <= style.maximum_length
    )


def test_quality_gate_rejects_content_outside_frozen_length_tier() -> None:
    payload = type("Payload", (), {
        "grounding_enrollment_id": "enrollment-1",
        "grounding_snapshot_id": "snapshot-a",
        "target_ordinal": 1,
    })()
    style = frozen_comment_style("snapshot-a", 1)
    invalid = "一" * (style.maximum_length + 1)

    decision = _style_error(payload, invalid, audit={})

    assert decision is not None
    assert decision.code == "comment_length_tier_mismatch"

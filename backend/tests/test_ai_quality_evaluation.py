from __future__ import annotations

import pytest

from app.services.task_center.ai_quality_evaluation import (
    PreferenceSample,
    aggregate_position_swap,
    cluster_bootstrap_preference_interval,
)


pytestmark = pytest.mark.no_postgres


def test_position_swap_maps_both_orders_back_to_candidate() -> None:
    result = aggregate_position_swap(
        {"winner": "A", "confidence": 0.91, "evidence": ["更贴合上下文"]},
        {"winner": "B", "confidence": 0.86, "evidence": ["声线更自然"]},
    )

    assert result.winner == "candidate"
    assert result.position_consistent is True
    assert result.confidence == 0.86


def test_position_swap_disagreement_is_low_confidence_tie() -> None:
    result = aggregate_position_swap(
        {"winner": "A", "confidence": 0.9},
        {"winner": "A", "confidence": 0.9},
    )

    assert result.winner == "tie"
    assert result.position_consistent is False
    assert result.confidence == 0.0


def test_cluster_bootstrap_reports_ties_and_context_cluster_interval() -> None:
    samples = [
        PreferenceSample("context-a", "candidate"),
        PreferenceSample("context-a", "candidate"),
        PreferenceSample("context-b", "baseline"),
        PreferenceSample("context-c", "candidate"),
        PreferenceSample("context-c", "tie"),
    ]

    result = cluster_bootstrap_preference_interval(
        samples,
        seed="evaluation-v1",
        iterations=500,
    )

    assert result.point_estimate == 0.75
    assert result.effective_count == 4
    assert result.tie_count == 1
    assert result.cluster_count == 3
    assert 0 <= result.lower_95 <= result.upper_95 <= 1

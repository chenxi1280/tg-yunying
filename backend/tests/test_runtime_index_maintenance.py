from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.services.task_center.runtime_index_maintenance import (
    CAPACITY_SAFETY_MULTIPLIER,
    CREATE_AI_MEMORY_INDEX,
    NEW_AI_MEMORY_INDEX,
    VACUUM_ANALYZE_ACTIONS,
    IndexState,
    _state_fingerprint,
    _validate_capacity,
)


pytestmark = pytest.mark.no_postgres


def test_ai_memory_candidate_index_is_account_aware_without_text_include() -> None:
    assert f"CREATE INDEX CONCURRENTLY {NEW_AI_MEMORY_INDEX}" in CREATE_AI_MEMORY_INDEX
    assert "(tenant_id, account_id, updated_at DESC)" in CREATE_AI_MEMORY_INDEX
    assert "INCLUDE (planned_at, status, id)" in CREATE_AI_MEMORY_INDEX
    assert "normalized_text" not in CREATE_AI_MEMORY_INDEX
    assert "raw_text" not in CREATE_AI_MEMORY_INDEX


def test_actions_vacuum_is_online_reuse_not_full_rewrite() -> None:
    assert VACUUM_ANALYZE_ACTIONS == "VACUUM (ANALYZE) actions"
    assert "FULL" not in VACUUM_ANALYZE_ACTIONS


def test_index_state_fingerprint_detects_validity_or_size_drift() -> None:
    baseline = [IndexState("idx", True, True, True, 100)]

    assert _state_fingerprint(baseline) != _state_fingerprint([
        IndexState("idx", True, False, True, 100),
    ])
    assert _state_fingerprint(baseline) != _state_fingerprint([
        IndexState("idx", True, True, True, 101),
    ])


def test_capacity_gate_requires_fresh_three_times_index_size() -> None:
    observed_at = datetime.now(timezone.utc)
    _validate_capacity(300, observed_at, 100)

    with pytest.raises(RuntimeError, match="runtime_index_capacity_insufficient"):
        _validate_capacity(100 * CAPACITY_SAFETY_MULTIPLIER - 1, observed_at, 100)
    with pytest.raises(ValueError, match="runtime_index_capacity_evidence_stale"):
        _validate_capacity(300, observed_at - timedelta(hours=1), 100)

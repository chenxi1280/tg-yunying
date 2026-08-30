from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.services.task_center.runtime_retention_policy import (
    DEFAULT_RUNTIME_ACTION_RETENTION_POLICY,
    RetentionCandidate,
    candidate_fingerprint,
    configured_runtime_action_retention_policy,
    terminal_reason_code,
)


pytestmark = pytest.mark.no_postgres


def test_policy_uses_state_specific_complete_natural_days() -> None:
    as_of = datetime(2026, 8, 30, 22, 30, tzinfo=timezone.utc)

    cutoffs = DEFAULT_RUNTIME_ACTION_RETENTION_POLICY.cutoffs(as_of)

    assert cutoffs["skipped"].isoformat() == "2026-08-29T16:00:00+00:00"
    assert cutoffs["success"].isoformat() == "2026-08-28T16:00:00+00:00"
    assert cutoffs["failed"].isoformat() == "2026-08-23T16:00:00+00:00"
    assert set(cutoffs) == {"skipped", "success", "failed"}


def test_policy_treats_application_naive_datetime_as_beijing_wall_clock() -> None:
    cutoffs = DEFAULT_RUNTIME_ACTION_RETENTION_POLICY.cutoffs(datetime(2026, 8, 31, 6, 0))

    assert cutoffs["skipped"].isoformat() == "2026-08-29T16:00:00+00:00"


def test_rollout_hold_prevents_short_ttl_before_explicit_activation() -> None:
    hold = configured_runtime_action_retention_policy(
        enabled=False,
        skipped_days=1,
        success_days=2,
        failed_days=7,
    )
    active = configured_runtime_action_retention_policy(
        enabled=True,
        skipped_days=1,
        success_days=2,
        failed_days=7,
    )

    assert hold.retention_days() == {"skipped": 5, "success": 5, "failed": 7}
    assert active.retention_days() == {"skipped": 1, "success": 2, "failed": 7}


@pytest.mark.parametrize(
    ("result", "attempt_failure", "expected"),
    [
        ({"reason_code": "obligation_not_open", "error_code": "ignored"}, "ignored", "obligation_not_open"),
        ({"error_code": "context_freshness_unproven"}, "ignored", "context_freshness_unproven"),
        ({"failure_type": "provider_known_failure"}, "ignored", "provider_known_failure"),
        ({"skip_reason": "duplicate_before_send"}, "ignored", "duplicate_before_send"),
        ({"generation_outcome": "ready"}, "ignored", "ready"),
        ({}, "gateway_timeout", "gateway_timeout"),
        ({"error_code": "contains spaces/文本"}, "", "contains_spaces"),
        ({}, "", "unclassified"),
    ],
)
def test_terminal_reason_code_uses_only_typed_bounded_values(
    result: dict,
    attempt_failure: str,
    expected: str,
) -> None:
    assert terminal_reason_code(result, attempt_failure) == expected


def test_candidate_fingerprint_is_order_independent_and_drift_sensitive() -> None:
    first = RetentionCandidate(
        id="b",
        status="success",
        age_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
        action_type="send_message",
        reason_code="ready",
    )
    second = RetentionCandidate(
        id="a",
        status="skipped",
        age_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
        action_type="send_message",
        reason_code="obligation_not_open",
    )

    fingerprint = candidate_fingerprint([first, second])

    assert candidate_fingerprint([second, first]) == fingerprint
    assert candidate_fingerprint([first, SimpleNamespace(**{**second.__dict__, "status": "failed"})]) != fingerprint

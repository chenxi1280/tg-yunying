from __future__ import annotations

import pytest

from scripts.migrate_ai_group_daily_targets import migrated_open_payload, normalized_task_config


@pytest.mark.no_postgres
def test_legacy_per_account_two_migrates_to_frozen_account_count() -> None:
    config = normalized_task_config(
        {
            "per_account_daily_min_messages": 2,
            "per_account_daily_max_messages": 2,
            "hard_hourly_target_enabled": True,
            "hourly_min_messages": 10,
        },
        frozen_account_count=7,
    )

    assert config["daily_message_target"] == 7
    assert config["account_coverage_mode"] == "all_accounts_daily"
    assert "per_account_daily_min_messages" not in config
    assert "hard_hourly_target_enabled" not in config


@pytest.mark.no_postgres
def test_explicit_daily_target_is_preserved_above_account_floor() -> None:
    config = normalized_task_config({"daily_message_target": 12}, frozen_account_count=7)

    assert config["daily_message_target"] == 12


@pytest.mark.no_postgres
def test_coverage_bound_hard_action_is_rebound_without_hour_bucket() -> None:
    payload, decision = migrated_open_payload(
        {
            "coverage_ledger_id": "coverage-1",
            "hard_hourly_target": True,
            "hard_hourly_bucket": "2026-07-28T10:00:00",
            "hard_hourly_goal_at_plan": 10,
        },
        coverage_bound=True,
    )

    assert decision == "rebound_daily_target"
    assert "hard_hourly_target" not in payload
    assert "hard_hourly_bucket" not in payload


@pytest.mark.no_postgres
def test_unbound_hard_action_is_not_reused_for_daily_volume() -> None:
    payload, decision = migrated_open_payload(
        {"hard_hourly_target": True, "hard_hourly_bucket": "2026-07-28T10:00:00"},
        coverage_bound=False,
    )

    assert decision == "skip_unbound_hard_target"
    assert payload["hard_hourly_target"] is True

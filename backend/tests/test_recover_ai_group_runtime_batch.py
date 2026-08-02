from __future__ import annotations

from types import SimpleNamespace

import pytest

from scripts.recover_ai_group_runtime_batch import proposed_config, snapshot_hash


pytestmark = pytest.mark.no_postgres


def test_snapshot_hash_is_order_independent() -> None:
    assert snapshot_hash({"b": 2, "a": 1}) == snapshot_hash({"a": 1, "b": 2})


def test_proposed_config_preserves_daily_target_contract() -> None:
    task = SimpleNamespace(type_config={
        "target_group_id": 1,
        "daily_message_target": 800,
        "account_coverage_mode": "all_accounts_daily",
        "messages_per_round_mode": "manual",
        "messages_per_round": 60,
        "reply_min_per_round": 12,
    })
    request = SimpleNamespace(messages_per_round=1, reply_min_per_round=1)

    result = proposed_config(task, request)

    assert result["daily_message_target"] == 800
    assert result["account_coverage_mode"] == "all_accounts_daily"
    assert result["messages_per_round"] == 1
    assert result["reply_min_per_round"] == 1

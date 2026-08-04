from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = PROJECT_ROOT / ".github/scripts/task_fulfillment_e4_diagnostics.py"
pytestmark = pytest.mark.no_postgres


def load_module():
    spec = importlib.util.spec_from_file_location("task_fulfillment_e4_diagnostics", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_group_daily_snapshot_requires_due_coverage_and_new_remote_fact():
    module = load_module()
    snapshot = {
        "task_id": "ai-task",
        "task_type": "group_ai_chat",
        "task_status": "running",
        "ledger_id": "ledger-ai",
        "planner_runtime_error": None,
        "attempts": {"post_release_remote_success_count": 1},
        "group_daily": {
            "target_row_count": 1,
            "due_message_count": 3,
            "confirmed_message_count": 3,
            "coverage_required_count": 2,
            "coverage_confirmed_count": 2,
        },
    }

    assert module.e4_blockers(snapshot) == []

    snapshot["group_daily"]["confirmed_message_count"] = 2
    snapshot["attempts"]["post_release_remote_success_count"] = 0
    assert module.e4_blockers(snapshot) == [
        "ai_daily_due_unmet",
        "ai_post_release_remote_fact_missing",
    ]


def test_search_and_view_snapshots_require_typed_remote_evidence():
    module = load_module()
    search = {
        "task_id": "search-task",
        "task_type": "search_click",
        "task_status": "running",
        "ledger_id": "ledger-search",
        "planner_runtime_error": None,
        "search_click": {"required_count": 2, "confirmed_count": 1, "post_release_confirmed_count": 0},
    }
    view = {
        "task_id": "view-task",
        "task_type": "channel_view",
        "task_status": "running",
        "ledger_id": "ledger-view",
        "planner_runtime_error": None,
        "channel_view": {"required_count": 1, "confirmed_count": 1, "remote_fact_count": 0, "post_release_remote_fact_count": 0},
    }

    assert module.e4_blockers(search) == ["search_click_unmet", "search_click_post_release_fact_missing"]
    assert module.e4_blockers(view) == ["channel_view_remote_fact_missing", "channel_view_post_release_fact_missing"]


def test_missing_ledger_and_current_planner_error_fail_closed():
    module = load_module()
    snapshot = {
        "task_id": "task-x",
        "task_type": "group_ai_chat",
        "task_status": "running",
        "ledger_id": None,
        "planner_runtime_error": {"message": "planner exploded"},
        "attempts": {"post_release_remote_success_count": 0},
        "group_daily": {},
    }

    assert module.e4_blockers(snapshot) == ["task_day_ledger_missing", "planner_runtime_error"]


def test_business_attempts_are_scoped_to_the_task_action_type():
    module = load_module()

    assert module.BUSINESS_ACTION_TYPES == {
        "group_ai_chat": "send_message",
        "search_click": "search_join",
        "channel_view": "view_message",
    }

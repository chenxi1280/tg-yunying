from __future__ import annotations

import importlib.util
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = PROJECT_ROOT / ".github/scripts/recent_group_ai_task_diagnostics.py"


def _module():
    spec = importlib.util.spec_from_file_location(
        "recent_group_ai_task_diagnostics", SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_classify_recent_group_ai_first_boundaries():
    module = _module()
    base = {
        "ledger": {"id": "ledger-1"},
        "daily": {"due_count": 20, "confirmed_count": 0},
        "actions": [],
        "remote": {
            "attempt_count": 0,
            "remote_message_count": 0,
        },
    }

    assert module.classify_first_boundary(base) == "planner_materialization"
    base["actions"] = [
        {"status": "pending", "generation_status": "generating"}
    ]
    assert module.classify_first_boundary(base) == "generation"
    base["actions"] = [{"status": "pending", "generation_status": "ready"}]
    assert module.classify_first_boundary(base) == "dispatcher_pre_gateway"
    base["remote"]["attempt_count"] = 1
    assert module.classify_first_boundary(base) == "gateway_remote_result"
    base["remote"]["remote_message_count"] = 1
    assert module.classify_first_boundary(base) == "active_remote_sending"


def test_recent_group_ai_query_is_running_and_recent_first():
    text = SCRIPT_PATH.read_text()

    assert "type = 'group_ai_chat'" in text
    assert "status = 'running'" in text
    assert "ORDER BY created_at DESC" in text
    assert "post_release_remote_message_count" in text
    assert "typed_remote_fact_count" in text


def test_release_anchor_without_offset_uses_business_timezone():
    module = _module()

    parsed = module.parse_release_live_at("2026-08-11 02:15:57")

    assert parsed.isoformat() == "2026-08-11T02:15:57+08:00"

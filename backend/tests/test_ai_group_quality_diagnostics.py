from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = PROJECT_ROOT / ".github/scripts/ai_group_quality_diagnostics.py"
pytestmark = pytest.mark.no_postgres


def load_quality_diagnostics_module():
    spec = importlib.util.spec_from_file_location("ai_group_quality_diagnostics", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ai_group_quality_diagnostics_blocks_stale_online_state():
    module = load_quality_diagnostics_module()

    blockers = module.online_gate_blockers(
        [
            {
                "task_id": "task-ai",
                "name": "郑州楼凤",
                "status": "running",
                "online_summary": {
                    "desired_count": 10,
                    "online_count": 9,
                    "stale_count": 1,
                    "missing_state_count": 0,
                    "blocked_count": 0,
                    "relogin_required_count": 0,
                    "offline_count": 0,
                },
            }
        ]
    )

    assert blockers == [
        {
            "task_id": "task-ai",
            "name": "郑州楼凤",
            "status": "running",
            "desired_count": 10,
            "online_count": 9,
            "non_online_count": 1,
            "stale_count": 1,
            "missing_state_count": 0,
            "blocked_count": 0,
            "relogin_required_count": 0,
            "offline_count": 0,
        }
    ]


def test_ai_group_quality_diagnostics_accepts_fully_online_state():
    module = load_quality_diagnostics_module()

    blockers = module.online_gate_blockers(
        [
            {
                "task_id": "task-ai",
                "name": "郑州楼凤",
                "status": "running",
                "online_summary": {
                    "desired_count": 10,
                    "online_count": 10,
                    "stale_count": 0,
                    "missing_state_count": 0,
                    "blocked_count": 0,
                    "relogin_required_count": 0,
                    "offline_count": 0,
                },
            }
        ]
    )

    assert blockers == []


def test_ai_group_quality_diagnostics_blocks_recent_effective_duplicate_text():
    module = load_quality_diagnostics_module()
    actions = [
        SimpleNamespace(id="a1", status="success", payload={"message_text": "嫩是真嫩 就是不知道稳不稳"}),
        SimpleNamespace(id="a2", status="pending", payload={"message_text": "嫩是真嫩 就是不知道稳不稳"}),
        SimpleNamespace(id="a3", status="failed", payload={"message_text": "没发送成功不用阻断"}),
        SimpleNamespace(id="a4", status="skipped", payload={"message_text": "没发送成功不用阻断"}),
    ]

    snapshot = module.recent_action_duplicate_summary(actions)

    assert snapshot["duplicate_blockers"] == [
        {
            "text": "嫩是真嫩 就是不知道稳不稳",
            "effective_count": 2,
            "status_counts": {"pending": 1, "success": 1},
            "action_ids": ["a1", "a2"],
        }
    ]


def test_ai_group_quality_diagnostics_reports_success_only_duplicates_without_blocking():
    module = load_quality_diagnostics_module()
    actions = [
        SimpleNamespace(id="a1", status="success", payload={"message_text": "已发历史重复"}),
        SimpleNamespace(id="a2", status="success", payload={"message_text": "已发历史重复"}),
    ]

    snapshot = module.recent_action_duplicate_summary(actions)

    assert snapshot["sent_duplicate_observations"] == [
        {
            "text": "已发历史重复",
            "sent_count": 2,
            "status_counts": {"success": 2},
            "action_ids": ["a1", "a2"],
        }
    ]
    assert snapshot["duplicate_blockers"] == []


def test_ai_group_quality_diagnostics_ignores_failed_only_duplicate_text():
    module = load_quality_diagnostics_module()
    actions = [
        SimpleNamespace(id="a1", status="failed", payload={"message_text": "失败文本重复"}),
        SimpleNamespace(id="a2", status="skipped", payload={"message_text": "失败文本重复"}),
    ]

    snapshot = module.recent_action_duplicate_summary(actions)

    assert snapshot["repeated_texts"] == [{"text": "失败文本重复", "count": 2}]
    assert snapshot["duplicate_blockers"] == []

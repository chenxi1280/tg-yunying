from __future__ import annotations

import importlib.util
from pathlib import Path

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

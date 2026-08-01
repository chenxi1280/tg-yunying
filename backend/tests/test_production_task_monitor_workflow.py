from pathlib import Path

import pytest


pytestmark = pytest.mark.no_postgres
ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/production-task-monitor.yml"


def test_monitor_is_read_only_and_requires_release_anchor() -> None:
    text = WORKFLOW.read_text()

    assert "release_live_at:" in text
    assert "deployed_sha:" in text
    assert "required: true" in text
    assert "readlink -f" in text
    assert "EXPECTED_SHA" in text
    assert "docker inspect" in text
    assert "tgyunying-worker-dispatcher-1" in text
    assert "task_fulfillment_e4_diagnostics.py" in text
    assert "drain_task_planner" not in text
    assert "docker restart" not in text
    assert "docker compose" not in text
    assert "docker pull" not in text


def test_monitor_has_no_push_or_schedule_trigger() -> None:
    text = WORKFLOW.read_text()

    assert "workflow_dispatch:" in text
    assert "push:" not in text
    assert "schedule:" not in text

from pathlib import Path

import pytest


pytestmark = pytest.mark.no_postgres
ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/production-task-monitor.yml"
TARGET_TUNING_WORKFLOW = ROOT / ".github/workflows/production-ai-group-target-tuning.yml"
AI_DISPATCH_DIAGNOSTIC = ROOT / ".github/scripts/ai_dispatch_admission_diagnostics.py"


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


def test_monitor_requires_explicit_incident_task_ids() -> None:
    text = WORKFLOW.read_text()
    task_ids_block = text.split("task_ids:", maxsplit=1)[1].split("type: string", maxsplit=1)[0]

    assert "required: true" in task_ids_block
    assert "default:" not in task_ids_block


def test_target_tuning_requires_and_verifies_deployed_sha() -> None:
    text = TARGET_TUNING_WORKFLOW.read_text()
    deployed_sha_block = text.split("deployed_sha:", maxsplit=1)[1].split("target:", maxsplit=1)[0]

    assert "default:" not in deployed_sha_block
    assert "EXPECTED_DEPLOYED_SHA: ${{ inputs.deployed_sha }}" in text
    assert "readlink -f" in text
    assert '[[ "${EXPECTED_DEPLOYED_SHA}" =~ ^[0-9a-fA-F]{40}$ ]]' in text
    assert '*_"${EXPECTED_SHORT_SHA}"' in text


def test_ai_route_diagnostic_uses_current_route_schema() -> None:
    text = AI_DISPATCH_DIAGNOSTIC.read_text()
    route_query = text.split("AI_ROUTES_QUERY = text", maxsplit=1)[1].split("def main", maxsplit=1)[0]

    assert "rs.purpose AS route_set_purpose" in route_query
    assert "rs.revision AS route_set_revision" in route_query
    assert "ri.model_name" in route_query
    assert "rs.name" not in route_query
    assert "ri.model_override" not in route_query

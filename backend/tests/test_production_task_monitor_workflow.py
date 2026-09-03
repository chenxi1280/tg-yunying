from pathlib import Path

import pytest


pytestmark = pytest.mark.no_postgres
ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/production-task-monitor.yml"
TARGET_TUNING_WORKFLOW = ROOT / ".github/workflows/production-ai-group-target-tuning.yml"
AI_DISPATCH_DIAGNOSTIC = ROOT / ".github/scripts/ai_dispatch_admission_diagnostics.py"
ROUTE_V2_SCRIPT = ROOT / "backend/scripts/enable_ai_route_v2_for_all_tasks.py"


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


def test_monitor_scopes_channel_interaction_diagnostics_to_incident_tasks() -> None:
    text = WORKFLOW.read_text()
    step = text.split("Read channel comment, like, and view E4 facts", maxsplit=1)[1]
    step = step.split("Read search click recent solver epochs", maxsplit=1)[0]

    assert "TASK_IDS: ${{ inputs.task_ids }}" in step
    assert "TASK_FULFILLMENT_E4_TASK_IDS='${TASK_IDS}'" in step


def test_target_tuning_requires_and_verifies_deployed_sha() -> None:
    text = TARGET_TUNING_WORKFLOW.read_text()
    deployed_sha_block = text.split("deployed_sha:", maxsplit=1)[1].split("target:", maxsplit=1)[0]

    assert "default:" not in deployed_sha_block
    assert "EXPECTED_DEPLOYED_SHA: ${{ inputs.deployed_sha }}" in text
    assert "readlink -f" in text
    assert '[[ "${EXPECTED_DEPLOYED_SHA}" =~ ^[0-9a-fA-F]{40}$ ]]' in text
    assert '*_"${EXPECTED_SHORT_SHA}"' in text


def test_target_tuning_preview_and_scope_are_forwarded_to_safe_scripts() -> None:
    text = TARGET_TUNING_WORKFLOW.read_text()
    task_id_block = text.split("task_id:", maxsplit=1)[1].split("target:", maxsplit=1)[0]

    assert "required: false" in task_id_block
    assert "update_ai_group_daily_targets.py" in text
    assert "enable_ai_route_v2_for_all_tasks.py" in text
    assert "--target ${TARGET} ${APPLY_FLAG} ${TASK_FLAG}" in text
    assert "${APPLY_FLAG} ${TASK_FLAG}" in text
    assert 'if [[ "${SWITCH_V2}" == "true" ]]' in text


def test_target_tuning_has_no_unscoped_inline_production_mutation() -> None:
    text = TARGET_TUNING_WORKFLOW.read_text()

    assert "a52e84f2-8663-4b00-bbbe-196fb626b28d" not in text
    assert "session.commit()" not in text
    assert ".values(can_send=True" not in text
    assert "SUCCESS_ZHENGZHOU_DAXUE_WOKEN_UP" not in text


def test_route_v2_script_accepts_the_same_optional_task_scope() -> None:
    text = ROUTE_V2_SCRIPT.read_text()

    assert 'parser.add_argument("--task-id"' in text
    assert "if task_id:" in text
    assert "query = query.where(Task.id == task_id)" in text
    assert "validated_type_config" in text
    assert "validate_task_ai_content_config" in text
    assert "activate_task_ai_content_config" in text
    assert "query = query.with_for_update()" in text
    assert 'config["ai_provider_id"]' not in text


def test_ai_route_diagnostic_uses_current_route_schema() -> None:
    text = AI_DISPATCH_DIAGNOSTIC.read_text()
    route_query = text.split("AI_ROUTES_QUERY = text", maxsplit=1)[1].split("def main", maxsplit=1)[0]

    assert "rs.purpose AS route_set_purpose" in route_query
    assert "rs.revision AS route_set_revision" in route_query
    assert "ri.model_name" in route_query
    assert "rs.name" not in route_query
    assert "ri.model_override" not in route_query


def test_ai_dispatch_today_success_query_uses_action_schema_columns() -> None:
    text = AI_DISPATCH_DIAGNOSTIC.read_text()
    success_query = text.split("TODAY_SUCCESS_QUERY = text", maxsplit=1)[1].split(
        "def main",
        maxsplit=1,
    )[0]

    assert "a.executed_at" in success_query
    assert "a.completed_at" not in success_query


def test_ai_dispatch_task_scope_query_uses_coverage_date_column() -> None:
    text = AI_DISPATCH_DIAGNOSTIC.read_text()
    task_scope_query = text.split("TASK_MEMBERSHIP_AND_COVERAGE_QUERY = text", maxsplit=1)[1].split(
        "def main",
        maxsplit=1,
    )[0]

    assert "coverage_date = CURRENT_DATE" in task_scope_query
    assert "target_date = CURRENT_DATE" not in task_scope_query
    assert "targeted_at = CURRENT_DATE" not in task_scope_query

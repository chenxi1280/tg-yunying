from pathlib import Path

import pytest


pytestmark = pytest.mark.no_postgres
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_restart_observation_api_requires_versioned_operator_evidence() -> None:
    schema = (REPOSITORY_ROOT / "backend/app/schemas/operations.py").read_text(encoding="utf-8")
    router = (REPOSITORY_ROOT / "backend/app/api/routers/operations.py").read_text(encoding="utf-8")

    assert "class GroupBotAdmissionObservationRestartRequest" in schema
    assert '"/api/groups/{group_id}/group-bot-admissions/{account_id}/restart-observation"' in router
    assert "ensure_permission(current_user, \"targets.manage\")" in router
    assert "restart_admission_observation" in router
    assert "group_bot_admission_observation_restarted" in router

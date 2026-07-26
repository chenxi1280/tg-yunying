from pathlib import Path

import pytest


pytestmark = pytest.mark.no_postgres


def test_restart_observation_api_requires_versioned_operator_evidence() -> None:
    schema = Path("backend/app/schemas/operations.py").read_text(encoding="utf-8")
    router = Path("backend/app/api/routers/operations.py").read_text(encoding="utf-8")

    assert "class GroupBotAdmissionObservationRestartRequest" in schema
    assert '"/api/groups/{group_id}/group-bot-admissions/{account_id}/restart-observation"' in router
    assert "ensure_permission(current_user, \"targets.manage\")" in router
    assert "restart_admission_observation" in router
    assert "group_bot_admission_observation_restarted" in router

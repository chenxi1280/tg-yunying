from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.no_postgres
def test_group_ai_capacity_precheck_warnings_have_operator_labels() -> None:
    source = (PROJECT_ROOT / "frontend/src/app/views/taskCenterViewModel.ts").read_text()

    assert "daily_coverage_capacity_insufficient:" in source
    assert "hard_hourly_group_cooldown_insufficient:" in source

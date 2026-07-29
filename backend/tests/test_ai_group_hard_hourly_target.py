from __future__ import annotations

import pytest

from app.models import Task
from app.schemas import GroupAIChatTaskConfigUpdate, TaskSettingsUpdate
from app.services.task_center.hard_hourly import enabled


pytestmark = pytest.mark.no_postgres


def test_legacy_hard_hourly_config_never_enables_runtime_gate() -> None:
    task = Task(
        tenant_id=1,
        name="历史任务",
        type="group_ai_chat",
        type_config={
            "hard_hourly_target_enabled": True,
            "hourly_min_messages": 100,
            "hard_hourly_strategy": "force_planning",
        },
    )

    assert enabled(task) is False


@pytest.mark.parametrize(
    "model",
    [GroupAIChatTaskConfigUpdate, TaskSettingsUpdate],
)
def test_hard_hourly_fields_are_rejected_by_current_contract(model) -> None:
    with pytest.raises(ValueError):
        model(hard_hourly_target_enabled=True, hourly_min_messages=10)

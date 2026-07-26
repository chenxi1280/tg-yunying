from __future__ import annotations

import pytest

from app.models import Task
from app.services.task_center.executors.group_ai_chat import _plan_account_limit


pytestmark = pytest.mark.no_postgres


def _all_account_task() -> Task:
    return Task(
        id="batch-limit-task",
        tenant_id=1,
        name="AI 活群",
        type="group_ai_chat",
        account_config={"max_concurrent": 60},
        type_config={"account_coverage_mode": "all_accounts_daily"},
    )


def test_all_account_daily_planner_never_exceeds_twenty_transaction_slots() -> None:
    task = _all_account_task()

    assert _plan_account_limit(task, {}, planning_limit=60) == 20
    assert _plan_account_limit(task, {"deficit": 60}, planning_limit=60) == 20

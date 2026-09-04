from __future__ import annotations

from datetime import date
import pytest

from app.models import AccountBehaviorBudgetLedger
from app.services.task_center.engagement_runtime_resources import (
    RuntimeResourceBlocked,
    _assert_behavior_capacity,
)

pytestmark = pytest.mark.no_postgres


def test_behavior_budget_passive_does_not_starve_active():
    budgets = {
        "total": 60,
        "authored_content": 10,
        "visible_reaction": 50,
        "passive_operation": 20,
    }

    # Account has 45 reactions and 10 views (total = 55)
    # Neither reaction limit (50) nor view limit (20) is exceeded individually
    ledger = AccountBehaviorBudgetLedger(
        tenant_id=1,
        account_id=101,
        task_day=date(2026, 9, 4),
        policy_revision_id="pol-1",
        action_budgets=budgets,
        counters={
            "visible_reaction": {"confirmed": 45},
            "passive_operation": {"confirmed": 10},
            "authored_content": {"confirmed": 0},
        },
    )

    # Passive reaction should be blocked because total passive actions (55) hit the safe passive floor (60 - 10 = 50)
    with pytest.raises(RuntimeResourceBlocked) as exc_reaction:
        _assert_behavior_capacity(ledger, "visible_reaction")
    assert exc_reaction.value.code == "account_behavior_passive_budget_exhausted"

    # Passive view should also be blocked
    with pytest.raises(RuntimeResourceBlocked) as exc_view:
        _assert_behavior_capacity(ledger, "passive_operation")
    assert exc_view.value.code == "account_behavior_passive_budget_exhausted"

    # BUT High-priority authored_content (AI group chat message) MUST NOT be blocked!
    _assert_behavior_capacity(ledger, "authored_content")

    # Now if authored_content uses 5 actions (total = 60)
    ledger.counters["authored_content"] = {"confirmed": 5}
    with pytest.raises(RuntimeResourceBlocked) as exc_total:
        _assert_behavior_capacity(ledger, "authored_content")
    assert exc_total.value.code == "account_behavior_total_budget_exhausted"

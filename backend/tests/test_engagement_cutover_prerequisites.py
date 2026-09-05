from datetime import datetime, timezone

import pytest

from app.models import AccountGroupMembershipSnapshotSet, Tenant, TgAccount
from app.services.task_center.config_normalization import validated_type_config
from app.services.task_center.daily_ledgers import ensure_task_day_ledger
from app.services.task_center.engagement_participation import ensure_daily_participation_plan
from tests.test_engagement_participation import _seed, _session


pytestmark = pytest.mark.no_postgres


def test_existing_internal_group_config_survives_validation():
    data = {"target_group_id": 101, "ai_content_context_route": "general",
            "group_rescue_admin_account_id": 12}
    result = validated_type_config("group_ai_chat", data)
    assert result["ai_content_context_route"] == "general"
    assert result["group_rescue_admin_account_id"] == 12
    assert data == {"target_group_id": 101, "ai_content_context_route": "general",
                    "group_rescue_admin_account_id": 12}


@pytest.mark.parametrize("fields", [
    {"ai_content_context_route": "unknown-route"},
    {"group_rescue_admin_account_id": -1},
    {"group_rescue_admin_account_id": "invalid"},
    {"unknown_internal_field": "unexpected"},
])
def test_invalid_internal_group_config_is_rejected(fields):
    with pytest.raises(ValueError):
        validated_type_config("group_ai_chat", {"target_group_id": 101, **fields})


def test_participation_excludes_rescue_owners_without_hiding_unhealthy_members():
    with _session() as session:
        task = _seed(session)
        ledger = ensure_task_day_ledger(session, task,
                     now=datetime(2026, 9, 5, 3, tzinfo=timezone.utc))
        session.get(Tenant, 1).group_rescue_admin_account_id = 11
        session.get(TgAccount, 13).status = "离线"
        task.type = "group_ai_chat"
        task.type_config = {**task.type_config, "group_rescue_admin_account_id": 12}
        plan = ensure_daily_participation_plan(session, task, ledger)
        snapshot = session.get(AccountGroupMembershipSnapshotSet, plan.membership_snapshot_set_id)
        assert snapshot.member_account_ids == [11, 12, 13, 14]
        assert plan.policy_eligible_account_ids == [13, 14]
        assert set(plan.selected_account_ids) == {13, 14}
        assert plan.required_count == 2

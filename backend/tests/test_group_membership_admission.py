from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, inspect

from app.database import Base
from app.models import TaskMembershipAdmissionItem
from app.schemas import GroupMembershipAdmissionTaskCreate


NOW = datetime(2026, 6, 16, 20, 0, 0)


def test_group_membership_admission_schema_accepts_required_config() -> None:
    payload = GroupMembershipAdmissionTaskCreate(
        name="天津准入",
        target_operation_target_id=485,
        account_group_ids=[1, 2],
        scheduled_start=NOW,
        scheduled_end=NOW + timedelta(hours=1),
        admission_pacing={"mode": "spread", "max_concurrent": 6, "per_minute": 12},
        test_message={"mode": "ai_random", "min_chars": 3, "max_chars": 12, "delete_after_send": True},
    )

    assert payload.target_operation_target_id == 485
    assert payload.account_group_ids == [1, 2]
    assert payload.admission_pacing.max_concurrent == 6
    assert payload.test_message.delete_after_send is True


def test_group_membership_admission_schema_requires_account_groups() -> None:
    with pytest.raises(ValidationError, match="account_group_ids 至少选择一个账号分组"):
        GroupMembershipAdmissionTaskCreate(
            name="天津准入",
            target_operation_target_id=485,
            account_group_ids=[],
            scheduled_start=NOW,
            scheduled_end=NOW + timedelta(hours=1),
        )


def test_group_membership_admission_schema_rejects_invalid_window() -> None:
    with pytest.raises(ValidationError, match="scheduled_end 必须晚于 scheduled_start"):
        GroupMembershipAdmissionTaskCreate(
            name="天津准入",
            target_operation_target_id=485,
            account_group_ids=[1],
            scheduled_start=NOW,
            scheduled_end=NOW,
        )


def test_group_membership_admission_item_table_is_registered() -> None:
    sqlite_engine = create_engine("sqlite:///:memory:", future=True)
    assert TaskMembershipAdmissionItem.__tablename__ == "task_membership_admission_items"
    Base.metadata.create_all(sqlite_engine)

    assert "task_membership_admission_items" in inspect(sqlite_engine).get_table_names()

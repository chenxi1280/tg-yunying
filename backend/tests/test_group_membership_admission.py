from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import AccountPool, OperationTarget, TaskMembershipAdmissionItem, Tenant, TgAccount, TgGroup
from app.schemas import GroupMembershipAdmissionTaskCreate
from app.services.task_center.membership_admission import (
    lock_membership_admission_snapshot,
    plan_membership_admission_actions,
    plan_membership_admission_test_messages,
    sync_membership_admission_items,
)
from app.services.task_center.service import create_and_start_group_membership_admission_task, create_group_membership_admission_task


NOW = datetime(2026, 6, 16, 20, 0, 0)


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return Session(engine)


def _admission_payload(**overrides) -> GroupMembershipAdmissionTaskCreate:
    data = {
        "name": "天津准入",
        "target_operation_target_id": 485,
        "account_group_ids": [1],
        "scheduled_start": NOW,
        "scheduled_end": NOW + timedelta(hours=1),
    }
    data.update(overrides)
    return GroupMembershipAdmissionTaskCreate(**data)


def _seed_snapshot_data(session: Session) -> None:
    session.add(Tenant(id=1, name="默认运营空间"))
    session.add_all(
        [
            AccountPool(id=1, tenant_id=1, name="一组"),
            AccountPool(id=2, tenant_id=1, name="二组"),
        ]
    )
    session.add(OperationTarget(id=485, tenant_id=1, target_type="group", tg_peer_id="-100485", title="天津"))
    session.add(TgGroup(id=485, tenant_id=1, tg_peer_id="-100485", title="天津"))
    session.add_all(
        [
            TgAccount(id=11, tenant_id=1, pool_id=1, display_name="账号11", phone_masked="11", status="在线"),
            TgAccount(id=12, tenant_id=1, pool_id=1, display_name="账号12", phone_masked="12", status="在线"),
            TgAccount(id=21, tenant_id=1, pool_id=2, display_name="账号21", phone_masked="21", status="在线"),
            TgAccount(id=31, tenant_id=1, pool_id=None, display_name="账号31", phone_masked="31", status="在线"),
        ]
    )
    session.commit()


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


def test_create_group_membership_admission_task_stores_draft_task() -> None:
    with _session() as session:
        _seed_snapshot_data(session)

        task = create_group_membership_admission_task(session, 1, _admission_payload(), "tester")

        assert task.type == "group_membership_admission"
        assert task.status == "draft"
        assert task.type_config["target_operation_target_id"] == 485
        assert task.type_config["account_group_ids"] == [1]


def test_create_and_start_group_membership_admission_task_starts_without_snapshot() -> None:
    with _session() as session:
        _seed_snapshot_data(session)

        task = create_and_start_group_membership_admission_task(session, 1, _admission_payload(), "tester")

        assert task.status == "running"
        assert session.scalars(select(TaskMembershipAdmissionItem).where(TaskMembershipAdmissionItem.task_id == task.id)).all() == []


def test_locks_snapshot_once_from_selected_account_pools() -> None:
    with _session() as session:
        _seed_snapshot_data(session)
        task = create_and_start_group_membership_admission_task(session, 1, _admission_payload(account_group_ids=[1, 2]), "tester")

        items = lock_membership_admission_snapshot(session, task)
        session.add(TgAccount(id=22, tenant_id=1, pool_id=2, display_name="账号22", phone_masked="22", status="在线"))
        second_items = lock_membership_admission_snapshot(session, task)

        assert [item.account_id for item in items] == [11, 12, 21]
        assert [item.account_id for item in second_items] == [11, 12, 21]
        assert task.stats["admission_snapshot_total"] == 3
        assert task.stats["admission_pending_count"] == 3


def test_plans_membership_actions_for_pending_snapshot_items() -> None:
    with _session() as session:
        _seed_snapshot_data(session)
        task = create_and_start_group_membership_admission_task(session, 1, _admission_payload(account_group_ids=[1]), "tester")
        lock_membership_admission_snapshot(session, task)

        actions = plan_membership_admission_actions(session, task, now=NOW)

        assert len(actions) == 2
        assert {action.account_id for action in actions} == {11, 12}
        assert all(action.action_type == "ensure_target_membership" for action in actions)
        assert all(action.payload["require_send"] is True for action in actions)
        items = session.scalars(select(TaskMembershipAdmissionItem).where(TaskMembershipAdmissionItem.task_id == task.id)).all()
        assert {item.phase for item in items} == {"joining"}
        assert all(item.membership_action_id for item in items)


def test_membership_success_moves_item_to_test_message_pending() -> None:
    with _session() as session:
        _seed_snapshot_data(session)
        task = create_and_start_group_membership_admission_task(session, 1, _admission_payload(), "tester")
        [item] = lock_membership_admission_snapshot(session, task)[:1]
        [action] = plan_membership_admission_actions(session, task, now=NOW, limit=1)
        action.status = "success"
        action.result = {"success": True, "membership_status": "joined"}
        session.commit()

        sync_membership_admission_items(session, task)

        session.refresh(item)
        assert item.phase == "test_message_pending"
        assert item.manual_required is False


def test_membership_permission_denied_marks_waiting_approval() -> None:
    with _session() as session:
        _seed_snapshot_data(session)
        task = create_and_start_group_membership_admission_task(session, 1, _admission_payload(), "tester")
        [item] = lock_membership_admission_snapshot(session, task)[:1]
        [action] = plan_membership_admission_actions(session, task, now=NOW, limit=1)
        action.status = "skipped"
        action.result = {"membership_status": "permission_denied", "error_message": "已提交入群申请，等待管理员审批"}
        session.commit()

        sync_membership_admission_items(session, task)

        session.refresh(item)
        assert item.phase == "waiting_approval"
        assert item.manual_required is True
        assert item.failure_type == "group_admin"


def test_membership_unrecoverable_failure_marks_failed() -> None:
    with _session() as session:
        _seed_snapshot_data(session)
        task = create_and_start_group_membership_admission_task(session, 1, _admission_payload(), "tester")
        [item] = lock_membership_admission_snapshot(session, task)[:1]
        [action] = plan_membership_admission_actions(session, task, now=NOW, limit=1)
        action.status = "failed"
        action.result = {"error_code": "账号不可用", "error_message": "session 已失效"}
        session.commit()

        sync_membership_admission_items(session, task)

        session.refresh(item)
        assert item.phase == "failed"
        assert item.manual_required is True
        assert item.failure_type == "account_unavailable"


def test_plans_test_message_actions_after_membership_success() -> None:
    with _session() as session:
        _seed_snapshot_data(session)
        task = create_and_start_group_membership_admission_task(session, 1, _admission_payload(), "tester")
        [item] = lock_membership_admission_snapshot(session, task)[:1]
        [action] = plan_membership_admission_actions(session, task, now=NOW, limit=1)
        action.status = "success"
        action.result = {"success": True, "membership_status": "joined"}
        sync_membership_admission_items(session, task)

        [send_action] = plan_membership_admission_test_messages(session, task, now=NOW, limit=1)

        session.refresh(item)
        assert item.phase == "testing_message"
        assert item.test_message_action_id == send_action.id
        assert send_action.action_type == "send_message"
        assert send_action.account_id == item.account_id
        assert send_action.payload["group_id"] == 485
        assert send_action.payload["operation_target_id"] == 485
        assert send_action.payload["ai_generation_status"] == "pending"
        assert send_action.payload["profile_scene"] == "group_membership_admission_test"


def test_test_message_success_completes_item() -> None:
    with _session() as session:
        _seed_snapshot_data(session)
        task = create_and_start_group_membership_admission_task(session, 1, _admission_payload(), "tester")
        [item] = lock_membership_admission_snapshot(session, task)[:1]
        [action] = plan_membership_admission_actions(session, task, now=NOW, limit=1)
        action.status = "success"
        action.result = {"success": True, "membership_status": "joined"}
        sync_membership_admission_items(session, task)
        [send_action] = plan_membership_admission_test_messages(session, task, now=NOW, limit=1)
        send_action.status = "success"
        send_action.payload = {**send_action.payload, "message_text": "签到一下", "ai_generation_status": "success"}
        send_action.result = {"success": True, "telegram_msg_id": "777"}
        session.commit()

        sync_membership_admission_items(session, task, now=NOW + timedelta(minutes=1))

        session.refresh(item)
        assert item.phase == "completed"
        assert item.test_message_text == "签到一下"
        assert item.test_message_id == "777"
        assert item.completed_at == NOW + timedelta(minutes=1)
        assert task.stats["admission_completed_count"] == 1


def test_test_message_failure_marks_item_failed() -> None:
    with _session() as session:
        _seed_snapshot_data(session)
        task = create_and_start_group_membership_admission_task(session, 1, _admission_payload(), "tester")
        [item] = lock_membership_admission_snapshot(session, task)[:1]
        [action] = plan_membership_admission_actions(session, task, now=NOW, limit=1)
        action.status = "success"
        action.result = {"success": True, "membership_status": "joined"}
        sync_membership_admission_items(session, task)
        [send_action] = plan_membership_admission_test_messages(session, task, now=NOW, limit=1)
        send_action.status = "failed"
        send_action.result = {"error_code": "群无权限", "error_message": "该账号不可向此群发送"}
        session.commit()

        sync_membership_admission_items(session, task)

        session.refresh(item)
        assert item.phase == "failed"
        assert item.failure_type == "test_message_failed"
        assert item.failure_detail == "该账号不可向此群发送"

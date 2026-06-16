from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import AccountPool, Action, OperationTarget, TaskMembershipAdmissionItem, Tenant, TgAccount, TgGroup
from app.schemas import GroupMembershipAdmissionTaskCreate
from app.services.task_center.membership_admission import (
    lock_membership_admission_snapshot,
    mark_membership_admission_manual_handled,
    membership_admission_failure_rows,
    plan_membership_admission_actions,
    plan_membership_admission_delete_messages,
    plan_membership_admission_test_messages,
    retry_failed_membership_admission_items,
    retry_membership_admission_item,
    sync_membership_admission_items,
)
from app.services.task_center.executors import build_task_plan
from app.services.task_center.service import create_and_start_group_membership_admission_task, create_group_membership_admission_task
from app.services.task_center.service import get_task_detail


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


def test_delete_after_send_creates_delete_action() -> None:
    with _session() as session:
        _seed_snapshot_data(session)
        payload = _admission_payload(test_message={"delete_after_send": True})
        task = create_and_start_group_membership_admission_task(session, 1, payload, "tester")
        [item] = lock_membership_admission_snapshot(session, task)[:1]
        [action] = plan_membership_admission_actions(session, task, now=NOW, limit=1)
        action.status = "success"
        action.result = {"success": True, "membership_status": "joined"}
        sync_membership_admission_items(session, task)
        [send_action] = plan_membership_admission_test_messages(session, task, now=NOW, limit=1)
        send_action.status = "success"
        send_action.payload = {**send_action.payload, "message_text": "签到一下", "ai_generation_status": "success"}
        send_action.result = {"success": True, "telegram_msg_id": "777"}
        sync_membership_admission_items(session, task)

        [delete_action] = plan_membership_admission_delete_messages(session, task, now=NOW)

        session.refresh(item)
        assert item.phase == "completed"
        assert item.delete_status == "deleting"
        assert item.delete_action_id == delete_action.id
        assert delete_action.action_type == "delete_message"
        assert delete_action.payload["message_id"] == "777"


def test_delete_action_success_marks_item_deleted() -> None:
    with _session() as session:
        _seed_snapshot_data(session)
        task = create_and_start_group_membership_admission_task(session, 1, _admission_payload(test_message={"delete_after_send": True}), "tester")
        [item] = lock_membership_admission_snapshot(session, task)[:1]
        item.phase = "completed"
        item.test_message_id = "777"
        item.delete_status = "delete_pending"
        [delete_action] = plan_membership_admission_delete_messages(session, task, now=NOW)
        delete_action.status = "success"
        delete_action.result = {"success": True}
        session.commit()

        sync_membership_admission_items(session, task)

        session.refresh(item)
        assert item.phase == "completed"
        assert item.delete_status == "deleted"
        assert item.failure_type == ""


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


def test_executor_build_plan_locks_snapshot_and_creates_membership_actions() -> None:
    with _session() as session:
        _seed_snapshot_data(session)
        task = create_and_start_group_membership_admission_task(session, 1, _admission_payload(account_group_ids=[1]), "tester")

        created = build_task_plan(session, task)

        items = session.scalars(select(TaskMembershipAdmissionItem).where(TaskMembershipAdmissionItem.task_id == task.id)).all()
        assert created == 2
        assert [item.account_id for item in items] == [11, 12]
        assert {item.phase for item in items} == {"joining"}


def test_executor_build_plan_creates_test_messages_after_membership_success() -> None:
    with _session() as session:
        _seed_snapshot_data(session)
        task = create_and_start_group_membership_admission_task(session, 1, _admission_payload(account_group_ids=[1]), "tester")
        build_task_plan(session, task)
        item = session.scalar(select(TaskMembershipAdmissionItem).where(TaskMembershipAdmissionItem.task_id == task.id, TaskMembershipAdmissionItem.account_id == 11))
        action = session.get(Action, item.membership_action_id)
        action.status = "success"
        action.result = {"success": True, "membership_status": "joined"}
        session.commit()

        created = build_task_plan(session, task)

        session.refresh(item)
        assert created == 1
        assert item.phase == "testing_message"
        assert item.test_message_action_id


def test_task_detail_exposes_membership_admission_items() -> None:
    with _session() as session:
        _seed_snapshot_data(session)
        task = create_and_start_group_membership_admission_task(session, 1, _admission_payload(account_group_ids=[1]), "tester")
        build_task_plan(session, task)

        detail = get_task_detail(session, 1, task.id)

        phase = detail["membership_admission_phase"]
        items = detail["membership_admission_items"]
        assert phase["snapshot_total"] == 2
        assert phase["joining_count"] == 2
        assert len(items) == 2
        assert items[0]["account_id"] == 11
        assert items[0]["phase"] == "joining"
        assert items[0]["membership_action_id"]
        assert items[0]["display_name"] == "账号11"


def test_retry_membership_admission_item_resets_failed_state() -> None:
    with _session() as session:
        _seed_snapshot_data(session)
        task = create_and_start_group_membership_admission_task(session, 1, _admission_payload(), "tester")
        [item] = lock_membership_admission_snapshot(session, task)[:1]
        item.phase = "failed"
        item.failure_type = "test_message_failed"
        item.failure_detail = "该账号不可向此群发送"
        item.membership_action_id = "old-membership"
        item.test_message_action_id = "old-test"
        item.delete_action_id = "old-delete"
        item.delete_status = "delete_failed"
        session.commit()

        updated = retry_membership_admission_item(session, 1, task.id, item.id)

        assert updated.phase == "pending"
        assert updated.membership_action_id is None
        assert updated.test_message_action_id is None
        assert updated.delete_action_id is None
        assert updated.delete_status == ""
        assert updated.failure_type == ""


def test_retry_failed_membership_admission_items_resets_only_failed_items() -> None:
    with _session() as session:
        _seed_snapshot_data(session)
        task = create_and_start_group_membership_admission_task(session, 1, _admission_payload(account_group_ids=[1]), "tester")
        items = lock_membership_admission_snapshot(session, task)
        items[0].phase = "failed"
        items[0].failure_type = "test_message_failed"
        items[1].phase = "waiting_approval"
        items[1].manual_required = True
        session.commit()

        count = retry_failed_membership_admission_items(session, 1, task.id)

        session.refresh(items[0])
        session.refresh(items[1])
        assert count == 1
        assert items[0].phase == "pending"
        assert items[1].phase == "waiting_approval"


def test_mark_membership_admission_manual_handled_requeues_item() -> None:
    with _session() as session:
        _seed_snapshot_data(session)
        task = create_and_start_group_membership_admission_task(session, 1, _admission_payload(), "tester")
        [item] = lock_membership_admission_snapshot(session, task)[:1]
        item.phase = "waiting_approval"
        item.manual_required = True
        item.failure_type = "group_admin"
        item.failure_detail = "等待管理员审批"
        session.commit()

        updated = mark_membership_admission_manual_handled(session, 1, task.id, item.id)

        assert updated.phase == "pending"
        assert updated.manual_required is False
        assert updated.failure_type == ""


def test_membership_admission_failure_rows_include_failed_and_manual_items() -> None:
    with _session() as session:
        _seed_snapshot_data(session)
        task = create_and_start_group_membership_admission_task(session, 1, _admission_payload(account_group_ids=[1]), "tester")
        items = lock_membership_admission_snapshot(session, task)
        items[0].phase = "failed"
        items[0].failure_type = "test_message_failed"
        items[0].failure_detail = "该账号不可向此群发送"
        items[1].phase = "waiting_approval"
        items[1].manual_required = True
        items[1].failure_type = "group_admin"
        session.commit()

        rows = membership_admission_failure_rows(session, 1, task.id)

        assert [row["account_id"] for row in rows] == ["11", "12"]
        assert rows[0]["failure_detail"] == "该账号不可向此群发送"
        assert rows[1]["manual_required"] == "true"

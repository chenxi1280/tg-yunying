from __future__ import annotations

from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Action, OperationTarget, Task, TaskMembershipAdmissionItem, Tenant, TgAccount
from app.services.task_center.membership_admission import sync_membership_admission_items


def test_membership_admission_unknown_after_send_waits_for_manual_confirmation() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = datetime(2026, 6, 25, 11, 0, 0)

    with Session(engine) as session:
        task, item = _seed_unknown_membership_item(session, now_value)
        session.commit()

        sync_membership_admission_items(session, task, now_value)

        refreshed = session.get(TaskMembershipAdmissionItem, item.id)

    assert refreshed is not None
    assert refreshed.phase == "waiting_approval"
    assert refreshed.manual_required is True
    assert refreshed.failure_type == "unknown_after_send"
    assert refreshed.failure_detail == "worker lost after gateway call"
    assert task.stats["admission_joining_count"] == 0
    assert task.stats["admission_failed_count"] == 0
    assert task.stats["admission_manual_required_count"] == 1


def test_membership_admission_unknown_test_message_waits_for_manual_confirmation() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = datetime(2026, 6, 25, 11, 0, 0)

    with Session(engine) as session:
        task, item = _seed_unknown_test_message_item(session, now_value)
        session.commit()

        sync_membership_admission_items(session, task, now_value)

        refreshed = session.get(TaskMembershipAdmissionItem, item.id)

    assert refreshed is not None
    assert refreshed.phase == "waiting_approval"
    assert refreshed.manual_required is True
    assert refreshed.failure_type == "unknown_after_send"
    assert refreshed.failure_detail == "test message result unknown"
    assert task.stats["admission_testing_message_count"] == 0
    assert task.stats["admission_failed_count"] == 0
    assert task.stats["admission_manual_required_count"] == 1


def test_membership_admission_unknown_rescue_action_is_not_left_pending() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = datetime(2026, 6, 25, 11, 0, 0)

    with Session(engine) as session:
        task, item = _seed_unknown_rescue_item(session, now_value)
        session.commit()

        sync_membership_admission_items(session, task, now_value)

        refreshed = session.get(TaskMembershipAdmissionItem, item.id)

    assert refreshed is not None
    assert refreshed.rescue_status == "unknown_after_send"
    assert refreshed.rescue_failure_detail == "rescue invite result unknown"


def test_membership_admission_unknown_delete_action_is_not_left_deleting() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = datetime(2026, 6, 25, 11, 0, 0)

    with Session(engine) as session:
        task, item = _seed_unknown_delete_item(session, now_value)
        session.commit()

        sync_membership_admission_items(session, task, now_value)

        refreshed = session.get(TaskMembershipAdmissionItem, item.id)

    assert refreshed is not None
    assert refreshed.phase == "completed"
    assert refreshed.delete_status == "unknown_after_send"
    assert refreshed.failure_type == "unknown_after_send"
    assert refreshed.failure_detail == "delete message result unknown"


def _seed_unknown_membership_item(session: Session, now_value: datetime) -> tuple[Task, TaskMembershipAdmissionItem]:
    session.add(Tenant(id=1, name="默认运营空间"))
    session.add(OperationTarget(id=21, tenant_id=1, target_type="group", tg_peer_id="-1007", title="目标群"))
    session.add(TgAccount(id=11, tenant_id=1, display_name="准入号", phone_masked="+861***0011", status="在线"))
    task = Task(
        id="task-admission-unknown",
        tenant_id=1,
        name="群聊准入",
        type="group_membership_admission",
        status="running",
        type_config={"target_operation_target_id": 21},
    )
    item = TaskMembershipAdmissionItem(
        tenant_id=1,
        task_id=task.id,
        account_id=11,
        target_id=21,
        phase="joining",
        membership_action_id="membership-unknown",
    )
    session.add_all([task, item, _unknown_membership_action(task, now_value)])
    return task, item


def _seed_unknown_test_message_item(session: Session, now_value: datetime) -> tuple[Task, TaskMembershipAdmissionItem]:
    session.add(Tenant(id=1, name="默认运营空间"))
    session.add(OperationTarget(id=21, tenant_id=1, target_type="group", tg_peer_id="-1007", title="目标群"))
    session.add(TgAccount(id=11, tenant_id=1, display_name="测试号", phone_masked="+861***0011", status="在线"))
    task = Task(id="task-test-message-unknown", tenant_id=1, name="群聊准入", type="group_membership_admission", status="running")
    item = TaskMembershipAdmissionItem(
        tenant_id=1,
        task_id=task.id,
        account_id=11,
        target_id=21,
        phase="testing_message",
        test_message_action_id="test-message-unknown",
    )
    session.add_all([task, item, _unknown_test_message_action(task, now_value)])
    return task, item


def _seed_unknown_rescue_item(session: Session, now_value: datetime) -> tuple[Task, TaskMembershipAdmissionItem]:
    session.add(Tenant(id=1, name="默认运营空间"))
    session.add(OperationTarget(id=21, tenant_id=1, target_type="group", tg_peer_id="-1007", title="目标群"))
    session.add(TgAccount(id=11, tenant_id=1, display_name="救援目标号", phone_masked="+861***0011", status="在线"))
    task = Task(id="task-rescue-unknown", tenant_id=1, name="群聊准入", type="group_membership_admission", status="running")
    item = TaskMembershipAdmissionItem(
        tenant_id=1,
        task_id=task.id,
        account_id=11,
        target_id=21,
        phase="waiting_approval",
        rescue_action_id="rescue-unknown",
        rescue_status="pending",
    )
    session.add_all([task, item, _unknown_rescue_action(task, now_value)])
    return task, item


def _seed_unknown_delete_item(session: Session, now_value: datetime) -> tuple[Task, TaskMembershipAdmissionItem]:
    session.add(Tenant(id=1, name="默认运营空间"))
    session.add(OperationTarget(id=21, tenant_id=1, target_type="group", tg_peer_id="-1007", title="目标群"))
    session.add(TgAccount(id=11, tenant_id=1, display_name="删除号", phone_masked="+861***0011", status="在线"))
    task = Task(id="task-delete-unknown", tenant_id=1, name="群聊准入", type="group_membership_admission", status="running")
    item = TaskMembershipAdmissionItem(
        tenant_id=1,
        task_id=task.id,
        account_id=11,
        target_id=21,
        phase="completed",
        delete_after_send=True,
        delete_action_id="delete-unknown",
        delete_status="deleting",
    )
    session.add_all([task, item, _unknown_delete_action(task, now_value)])
    return task, item


def _unknown_membership_action(task: Task, now_value: datetime) -> Action:
    return Action(
        id="membership-unknown",
        tenant_id=1,
        task_id=task.id,
        task_type=task.type,
        action_type="ensure_target_membership",
        account_id=11,
        status="unknown_after_send",
        result={"error_code": "unknown_after_send", "error_message": "worker lost after gateway call"},
        scheduled_at=now_value,
        executed_at=now_value,
    )


def _unknown_rescue_action(task: Task, now_value: datetime) -> Action:
    return Action(
        id="rescue-unknown",
        tenant_id=1,
        task_id=task.id,
        task_type=task.type,
        action_type="invite_group_account",
        account_id=99,
        status="unknown_after_send",
        result={"error_code": "unknown_after_send", "error_message": "rescue invite result unknown"},
        scheduled_at=now_value,
        executed_at=now_value,
    )


def _unknown_delete_action(task: Task, now_value: datetime) -> Action:
    return Action(
        id="delete-unknown",
        tenant_id=1,
        task_id=task.id,
        task_type=task.type,
        action_type="delete_message",
        account_id=11,
        status="unknown_after_send",
        result={"error_code": "unknown_after_send", "error_message": "delete message result unknown"},
        scheduled_at=now_value,
        executed_at=now_value,
    )


def _unknown_test_message_action(task: Task, now_value: datetime) -> Action:
    return Action(
        id="test-message-unknown",
        tenant_id=1,
        task_id=task.id,
        task_type=task.type,
        action_type="send_message",
        account_id=11,
        status="unknown_after_send",
        result={"error_code": "unknown_after_send", "error_message": "test message result unknown"},
        scheduled_at=now_value,
        executed_at=now_value,
    )

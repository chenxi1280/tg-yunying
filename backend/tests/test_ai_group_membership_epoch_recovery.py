from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    Action,
    AccountStatus,
    ExecutionAttempt,
    GroupAuthStatus,
    OperationTarget,
    Task,
    TaskAccountDailyCoverage,
    TaskMembershipAdmissionItem,
    Tenant,
    TgAccount,
    TgGroup,
)
from app.services.task_center.admission_epoch_recovery import (
    replan_stale_admission_actions,
)
from app.services.task_center.account_scope import (
    initialize_all_account_task_scope,
    reset_all_account_scope_for_target_change,
)
from app.services._common import _now
from app.services.task_center.fulfillment_remote_facts import persist_remote_fact
from app.services.task_center.group_rescue import trigger_group_rescue
from app.services.task_center.targets import group_from_reference
from app.services.task_center.executors import group_ai_chat


pytestmark = pytest.mark.no_postgres
NOW = datetime(2026, 8, 28, 12, 0)


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as current:
        _seed_base(current)
        current.commit()
        yield current


def test_membership_success_without_message_id_is_typed_fact(
    session: Session,
) -> None:
    action = _membership_action(status="success", result={
        "success": True,
        "membership_status": "joined",
    })
    session.add(action)
    session.add(_attempt(action.id, status="success"))
    session.flush()

    fact = persist_remote_fact(session, action)

    assert fact is not None
    assert fact.fact_kind == "membership_observed"
    assert fact.outcome["membership_status"] == "joined"


def test_membership_success_without_observed_status_remains_unknown(
    session: Session,
) -> None:
    action = _membership_action(status="success", result={"success": True})
    session.add(action)
    session.add(_attempt(action.id, status="success"))
    session.flush()

    fact = persist_remote_fact(session, action)

    assert fact is not None
    assert fact.fact_kind == "remote_outcome_unknown"


def test_rescue_success_is_target_membership_typed_fact(session: Session) -> None:
    action = Action(
        id="rescue-success",
        tenant_id=1,
        task_id="task-1",
        task_type="group_ai_chat",
        action_type="invite_group_account",
        account_id=101,
        scheduled_at=NOW,
        status="success",
        task_lifecycle_epoch=7,
        payload={"target_account_id": 102},
        result={"rescue_status": "invite_success", "membership_status": "joined"},
    )
    session.add_all([action, _attempt(action.id, status="success")])
    session.flush()

    fact = persist_remote_fact(session, action)

    assert fact is not None
    assert fact.fact_kind == "membership_observed"


def test_rescue_action_uses_current_task_epoch(session: Session) -> None:
    task = session.get(Task, "task-1")
    group = session.get(TgGroup, 11)

    result = trigger_group_rescue(
        session,
        task,
        group,
        trigger_account_id=102,
        trigger_reason="permission denied",
        operation_target_id=21,
    )

    assert result.action is not None
    assert result.action.task_lifecycle_epoch == 7


def test_zero_attempt_stale_membership_action_is_replanned_once(
    session: Session,
) -> None:
    task = session.get(Task, "task-1")
    old = _membership_action(status="pending", epoch=1)
    item = TaskMembershipAdmissionItem(
        tenant_id=1,
        task_id=task.id,
        account_id=102,
        target_id=21,
        phase="joining",
        membership_action_id=old.id,
    )
    session.add_all([old, item])
    session.flush()

    assert replan_stale_admission_actions(session, task=task) == 1
    assert replan_stale_admission_actions(session, task=task) == 0

    replacements = list(session.scalars(select(Action).where(
        Action.task_id == task.id,
        Action.status == "pending",
    )))
    assert len(replacements) == 1
    assert replacements[0].task_lifecycle_epoch == 7
    assert old.status == "skipped"
    assert old.result["error_code"] == "stale_lifecycle_epoch_replanned"
    assert item.membership_action_id == replacements[0].id


def test_stale_membership_action_with_attempt_is_not_replanned(
    session: Session,
) -> None:
    task = session.get(Task, "task-1")
    old = _membership_action(status="pending", epoch=1)
    session.add(old)
    session.add(_attempt(old.id, status="before_call"))
    session.flush()

    assert replan_stale_admission_actions(session, task=task) == 0
    assert old.status == "pending"


def test_target_and_group_mismatch_is_rejected(session: Session) -> None:
    mismatch = TgGroup(
        id=12,
        tenant_id=1,
        tg_peer_id="-10012",
        title="same title",
        can_send=True,
        auth_status=GroupAuthStatus.AUTHORIZED.value,
    )
    session.add(mismatch)
    session.flush()

    assert group_from_reference(
        session,
        1,
        group_id=12,
        operation_target_id=21,
    ) is None
    assert group_from_reference(
        session,
        1,
        group_id=11,
        operation_target_id=21,
    ).id == 11


def test_username_only_target_reaches_membership_gate(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(group_ai_chat, "_now", lambda: NOW)
    with Session(engine) as session:
        session.add(Tenant(id=2, name="tenant-2"))
        session.add(OperationTarget(
            id=31, tenant_id=2, title="public", target_type="group",
            tg_peer_id="public_name", username="public_name",
            can_send=True, auth_status=GroupAuthStatus.AUTHORIZED.value,
        ))
        session.add(TgAccount(
            id=201, tenant_id=2, display_name="account", phone_masked="201",
            status=AccountStatus.ACTIVE.value, session_ciphertext="session",
        ))
        task = Task(
            id="username-only", tenant_id=2, name="task", type="group_ai_chat",
            status="running", account_config={
                "selection_mode": "manual", "account_ids": [201],
            }, type_config={"target_operation_target_id": 31}, stats={},
        )
        session.add(task)
        session.commit()

        assert group_ai_chat.build_plan(session, task) == 1

        action = session.scalar(select(Action).where(
            Action.task_id == task.id,
            Action.action_type == "ensure_target_membership",
        ))
        assert action.payload["channel_target_id"] == 31


def test_target_change_resets_scope_and_abandons_old_coverage(
    session: Session,
) -> None:
    task = session.get(Task, "task-1")
    old_item, old_coverage = _seed_old_scope(session, task)
    _seed_new_target(session)
    task.type_config = {
        **dict(task.type_config or {}),
        "target_operation_target_id": 22,
        "target_group_id": 13,
    }

    assert reset_all_account_scope_for_target_change(session, task) == 1
    initialize_all_account_task_scope(session, task)

    assert old_item.target_id == 22
    assert old_item.phase == "pending"
    assert old_item.completed_at is None
    assert old_coverage.state == "abandoned_for_day"
    current_groups = set(session.scalars(select(TaskAccountDailyCoverage.group_id).where(
        TaskAccountDailyCoverage.task_id == task.id,
        TaskAccountDailyCoverage.coverage_date == _now().date(),
    )))
    assert current_groups == {11, 13}


def _seed_old_scope(
    session: Session,
    task: Task,
) -> tuple[TaskMembershipAdmissionItem, TaskAccountDailyCoverage]:
    item = TaskMembershipAdmissionItem(
        tenant_id=1, task_id=task.id, account_id=102, target_id=21,
        phase="completed", completed_at=NOW,
    )
    session.add(item)
    session.flush()
    coverage = TaskAccountDailyCoverage(
        tenant_id=1, task_id=task.id, group_id=11, account_id=102,
        membership_item_id=item.id, coverage_date=_now().date(), state="ready",
    )
    session.add(coverage)
    session.flush()
    return item, coverage


def _seed_new_target(session: Session) -> None:
    session.add_all([
        OperationTarget(
            id=22,
            tenant_id=1,
            target_type="group",
            tg_peer_id="-10022",
            title="new group",
            can_send=True,
            auth_status=GroupAuthStatus.AUTHORIZED.value,
        ),
        TgGroup(
            id=13,
            tenant_id=1,
            tg_peer_id="-10022",
            title="new group",
            can_send=True,
            auth_status=GroupAuthStatus.AUTHORIZED.value,
        ),
    ])
    session.flush()


def _seed_base(session: Session) -> None:
    _seed_tenant_and_accounts(session)
    _seed_task_target(session)


def _seed_tenant_and_accounts(session: Session) -> None:
    session.add(Tenant(
        id=1,
        name="tenant",
        group_rescue_enabled=True,
        group_rescue_admin_account_id=101,
    ))
    session.add_all([
        TgAccount(
            id=101,
            tenant_id=1,
            display_name="admin",
            phone_masked="101",
            username="admin",
            session_ciphertext="session-admin",
            status=AccountStatus.ACTIVE.value,
        ),
        TgAccount(
            id=102,
            tenant_id=1,
            display_name="member",
            phone_masked="102",
            username="member",
            session_ciphertext="session-member",
            status=AccountStatus.ACTIVE.value,
        ),
    ])


def _seed_task_target(session: Session) -> None:
    session.add(OperationTarget(
        id=21,
        tenant_id=1,
        target_type="group",
        tg_peer_id="-10011",
        title="same title",
        can_send=True,
        auth_status=GroupAuthStatus.AUTHORIZED.value,
    ))
    session.add(TgGroup(
        id=11,
        tenant_id=1,
        tg_peer_id="-10011",
        title="same title",
        can_send=True,
        auth_status=GroupAuthStatus.AUTHORIZED.value,
    ))
    session.add(Task(
        id="task-1",
        tenant_id=1,
        name="AI group",
        type="group_ai_chat",
        status="running",
        task_lifecycle_epoch=7,
        type_config={
            "target_operation_target_id": 21,
            "target_group_id": 11,
        },
    ))


def _membership_action(
    *,
    status: str,
    result: dict | None = None,
    epoch: int = 7,
) -> Action:
    result = dict(result or {})
    return Action(
        id=f"membership-{status}-{epoch}-{len(result)}",
        tenant_id=1,
        task_id="task-1",
        task_type="group_ai_chat",
        action_type="ensure_target_membership",
        account_id=102,
        scheduled_at=NOW,
        status=status,
        task_lifecycle_epoch=epoch,
        action_dedupe_key=f"dedupe-{status}-{epoch}-{len(result)}",
        payload={
            "channel_id": "-10011",
            "channel_target_id": 21,
            "target_type": "group",
            "require_send": True,
        },
        result=result,
    )


def _attempt(action_id: str, *, status: str) -> ExecutionAttempt:
    return ExecutionAttempt(
        tenant_id=1,
        action_id=action_id,
        account_id=102,
        attempt_no=1,
        status=status,
        before_call_at=NOW,
        gateway_call_started_at=NOW,
        after_call_at=NOW,
    )

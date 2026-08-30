from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ai_group_rescue_recovery_test_support import (
    NOW,
    _binding_evidence,
    _recovery_scope,
    _seed_base,
    _seed_recovery_accounts,
)
from app.database import Base
from app.models import (
    Action,
    AccountStatus,
    Task,
    TaskMembershipAdmissionItem,
    Tenant,
    TgAccount,
    TgGroup,
)
from app.services.task_center import dispatcher
from app.services.task_center.account_scope import initialize_all_account_task_scope
from app.services.task_center.ai_group_rescue_protected_recovery import (
    BindingEvidence,
    apply_binding_recovery,
    preview_binding_recovery,
)
from app.services.task_center.group_rescue import trigger_group_rescue


pytestmark = pytest.mark.no_postgres


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as current:
        _seed_base(current)
        current.commit()
        yield current


def test_rescue_action_prefers_exact_task_admin_override(session: Session) -> None:
    task = session.get(Task, "task-1")
    group = session.get(TgGroup, 11)
    session.add(TgAccount(
        id=103,
        tenant_id=1,
        display_name="target admin",
        phone_masked="103",
        username="target_admin",
        session_ciphertext="session-target-admin",
        status=AccountStatus.ACTIVE.value,
    ))
    task.type_config = {
        **dict(task.type_config or {}),
        "group_rescue_admin_account_id": 103,
    }
    session.flush()

    result = trigger_group_rescue(
        session,
        task,
        group,
        trigger_account_id=102,
        trigger_reason="permission denied",
        operation_target_id=21,
    )

    assert result.action is not None
    assert result.action.account_id == 103
    assert session.get(Tenant, 1).group_rescue_admin_account_id == 101


@pytest.mark.parametrize(
    ("old_admin_id", "error_message"),
    [
        (101, "旧救援账号无权限"),
        (103, "The provided user is not a mutual contact"),
    ],
)
def test_rescue_refresh_uses_exact_task_admin(
    session: Session,
    old_admin_id: int,
    error_message: str,
) -> None:
    _seed_recovery_accounts(session)
    task = session.get(Task, "task-1")
    task.type_config = {
        **dict(task.type_config or {}),
        "group_rescue_admin_account_id": 103,
    }
    action = Action(
        id="stale-rescue",
        tenant_id=1,
        task_id=task.id,
        task_type=task.type,
        action_type="invite_group_account",
        account_id=old_admin_id,
        scheduled_at=NOW,
        status="failed",
        task_lifecycle_epoch=7,
        payload={
            "group_id": 11,
            "operation_target_id": 21,
            "group_peer_id": "-10011",
            "target_account_id": 102,
            "target_account_ref": "@member",
            "trigger_account_id": 102,
            "trigger_task_id": task.id,
            "trigger_reason": "permission denied",
        },
        result={"rescue_status": "invite_failed", "error_message": error_message},
    )
    session.add(action)
    session.flush()

    result = trigger_group_rescue(
        session,
        task,
        session.get(TgGroup, 11),
        trigger_account_id=102,
        trigger_reason="permission denied",
        operation_target_id=21,
    )

    assert result.action is action
    assert action.status == "pending"
    assert action.account_id == 103


def test_dispatcher_reserves_exact_task_admin_override(session: Session) -> None:
    _seed_recovery_accounts(session)
    task = session.get(Task, "task-1")
    task.type_config = {
        **dict(task.type_config or {}),
        "group_rescue_admin_account_id": 103,
    }
    action = Action(
        id="ordinary-admin-action",
        tenant_id=1,
        task_id=task.id,
        task_type=task.type,
        action_type="send_message",
        account_id=103,
        scheduled_at=NOW,
        status="pending",
    )
    session.add(action)
    session.flush()

    assert dispatcher._is_reserved_rescue_admin_action(
        session,
        action,
        session.get(TgAccount, 103),
    )


def test_dispatcher_refreshes_stale_rescue_to_task_override(session: Session) -> None:
    _seed_recovery_accounts(session)
    task = session.get(Task, "task-1")
    task.type_config = {
        **dict(task.type_config or {}),
        "group_rescue_admin_account_id": 103,
    }
    action = Action(
        id="stale-rescue-admin",
        tenant_id=1,
        task_id=task.id,
        task_type=task.type,
        action_type="invite_group_account",
        account_id=101,
        scheduled_at=NOW,
        status="pending",
        task_lifecycle_epoch=7,
        payload={
            "group_id": 11,
            "operation_target_id": 21,
            "group_peer_id": "-10011",
            "target_account_id": 102,
            "target_account_ref": "@member",
            "trigger_account_id": 102,
            "trigger_task_id": task.id,
            "trigger_reason": "permission denied",
        },
    )
    session.add(action)
    session.flush()

    assert dispatcher._refresh_stale_invite_group_account_action(session, action)
    assert action.account_id == 103
    assert action.status == "pending"


def test_task_admin_override_is_excluded_only_from_exact_task_scope(
    session: Session,
) -> None:
    task = session.get(Task, "task-1")
    session.add(TgAccount(
        id=103,
        tenant_id=1,
        display_name="target admin",
        phone_masked="103",
        username="target_admin",
        session_ciphertext="session-target-admin",
        status=AccountStatus.ACTIVE.value,
    ))
    task.account_config = {"selection_mode": "all"}
    task.type_config = {
        **dict(task.type_config or {}),
        "account_coverage_mode": "all_accounts_daily",
        "group_rescue_admin_account_id": 103,
    }
    session.flush()

    initialize_all_account_task_scope(session, task, now=NOW)

    scoped = set(session.scalars(select(TaskMembershipAdmissionItem.account_id).where(
        TaskMembershipAdmissionItem.task_id == task.id,
    )))
    assert scoped == {102}


def test_protected_binding_updates_only_exact_task_and_preserves_tenant(
    session: Session,
) -> None:
    task = session.get(Task, "task-1")
    _seed_recovery_accounts(session)
    task.config_revision = 4
    session.flush()
    scope = _recovery_scope(config_revision=4)
    evidence = _binding_evidence()

    preview = preview_binding_recovery(session, scope, evidence)
    result = apply_binding_recovery(
        session,
        scope,
        evidence,
        expected_fingerprint=preview["fingerprint"],
        actor="operator",
        approval_reference="incident-1",
    )

    assert result["config_revision"] == 5
    assert task.type_config["group_rescue_admin_account_id"] == 103
    assert task.type_config["history_fetch_account_id"] == 104
    assert session.get(Tenant, 1).group_rescue_admin_account_id == 101


def test_protected_binding_rejects_candidate_with_open_action(
    session: Session,
) -> None:
    _seed_recovery_accounts(session)
    session.add(Action(
        id="candidate-open",
        tenant_id=1,
        task_id="task-1",
        task_type="group_ai_chat",
        action_type="send_message",
        account_id=103,
        scheduled_at=NOW,
        status="pending",
    ))
    session.flush()

    with pytest.raises(ValueError, match="rescue_admin_open_action"):
        preview_binding_recovery(
            session,
            _recovery_scope(config_revision=1),
            _binding_evidence(),
        )


def test_protected_binding_requires_complete_admin_rights(
    session: Session,
) -> None:
    _seed_recovery_accounts(session)
    evidence = BindingEvidence(
        **{
            **_binding_evidence().__dict__,
            "delete_messages": False,
        },
    )

    with pytest.raises(ValueError, match="remote_capability_missing"):
        preview_binding_recovery(
            session,
            _recovery_scope(config_revision=1),
            evidence,
        )

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    AccountEligibilityEvent,
    AccountPool,
    OperationTarget,
    Task,
    TaskMembershipAdmissionItem,
    Tenant,
    TgAccount,
    TgGroup,
)
from app.security import encrypt_session
from app.schemas.task_center import GroupAIChatTaskCreate
from app.services.task_center.account_scope import (
    eligible_account_ids,
    emit_account_eligibility_event,
    initialize_all_account_task_scope,
    process_account_eligibility_events,
)
from app.services.task_center.service import create_group_ai_chat_task


pytestmark = pytest.mark.no_postgres


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as current:
        yield current


def _seed_target(session: Session) -> tuple[TgGroup, OperationTarget]:
    group = TgGroup(id=21, tenant_id=1, tg_peer_id="-10021", title="目标群", active_window="09:00-23:00")
    target = OperationTarget(
        id=31, tenant_id=1, target_type="group", tg_peer_id="-10021", title="目标群",
        auth_status="已授权运营", can_send=True,
    )
    session.add_all([group, target])
    return group, target


def _task(task_id: str, *, selection_mode: str = "all") -> Task:
    return Task(
        id=task_id,
        tenant_id=1,
        name=task_id,
        type="group_ai_chat",
        status="running",
        account_config={"selection_mode": selection_mode, "account_ids": [1]},
        type_config={
            "target_group_id": 21,
            "target_operation_target_id": 31,
            "account_coverage_mode": "all_accounts_daily",
            "per_account_daily_min_messages": 1,
        },
    )


def _account(account_id: int, pool_id: int, *, identity: str = "normal", status: str = "在线", session_ready: bool = True) -> TgAccount:
    return TgAccount(
        id=account_id,
        tenant_id=1,
        pool_id=pool_id,
        display_name=f"账号{account_id}",
        phone_masked=f"***{account_id:04d}",
        account_identity=identity,
        status=status,
        session_ciphertext=encrypt_session(f"session-{account_id}") if session_ready else None,
    )


def _seed_scope_base(session: Session) -> None:
    tenant = Tenant(id=1, name="租户", group_rescue_admin_account_id=7)
    pools = [
        AccountPool(id=10, tenant_id=1, name="普通", pool_purpose="normal", is_enabled=True),
        AccountPool(id=11, tenant_id=1, name="接码", pool_purpose="code_receiver", system_key="code_receiver"),
        AccountPool(id=12, tenant_id=1, name="降权", pool_purpose="rank_deboost", system_key="rank_deboost"),
        AccountPool(id=13, tenant_id=1, name="停用普通", pool_purpose="normal", is_enabled=False),
    ]
    session.add(tenant)
    session.add_all(pools)
    _seed_target(session)


def test_eligible_account_ids_enforces_session_usage_and_rescue_boundaries(session: Session) -> None:
    _seed_scope_base(session)
    session.add_all([
        _account(1, 10),
        _account(2, 10, session_ready=False),
        _account(3, 10, status="受限"),
        _account(4, 11, identity="code_receiver"),
        _account(5, 12, identity="rank_deboost"),
        _account(6, 13),
        _account(7, 10),
    ])
    session.commit()

    assert eligible_account_ids(session, 1) == [1]


def test_initial_scope_snapshot_creates_only_all_account_task_relations(session: Session) -> None:
    _seed_scope_base(session)
    session.add_all([_account(1, 10), _account(2, 10), _task("all-task"), _task("manual-task", selection_mode="manual")])
    session.commit()

    all_result = initialize_all_account_task_scope(session, session.get(Task, "all-task"), now=datetime(2026, 7, 10, 10))
    manual_result = initialize_all_account_task_scope(session, session.get(Task, "manual-task"), now=datetime(2026, 7, 10, 10))
    session.commit()

    relations = list(session.scalars(select(TaskMembershipAdmissionItem).order_by(TaskMembershipAdmissionItem.account_id)))
    assert all_result.created_relations == 2
    assert manual_result.created_relations == 0
    assert [(item.task_id, item.account_id, item.target_id) for item in relations] == [
        ("all-task", 1, 31),
        ("all-task", 2, 31),
    ]


def test_legacy_all_account_task_with_natural_mode_is_included(session: Session) -> None:
    _seed_scope_base(session)
    legacy_task = _task("legacy-all-task")
    legacy_task.type_config = {**legacy_task.type_config, "account_coverage_mode": "natural"}
    session.add_all([_account(1, 10), legacy_task])
    session.commit()

    result = initialize_all_account_task_scope(session, legacy_task, now=datetime(2026, 7, 10, 10))

    assert result.created_relations == 1


def test_account_event_incrementally_syncs_only_changed_account(session: Session) -> None:
    _seed_scope_base(session)
    session.add_all([_account(1, 10), _task("all-task")])
    session.commit()
    initialize_all_account_task_scope(session, session.get(Task, "all-task"), now=datetime(2026, 7, 10, 10))
    session.commit()

    session.add(_account(2, 10))
    session.flush()
    emit_account_eligibility_event(session, 2, "login_ready")
    session.commit()

    assert process_account_eligibility_events(session, limit=10, now=datetime(2026, 7, 10, 11)) == 1
    assert process_account_eligibility_events(session, limit=10, now=datetime(2026, 7, 10, 11)) == 0
    session.commit()

    relations = list(session.scalars(select(TaskMembershipAdmissionItem).order_by(TaskMembershipAdmissionItem.account_id)))
    event = session.scalar(select(AccountEligibilityEvent))
    assert [item.account_id for item in relations] == [1, 2]
    assert event is not None and event.processed_at is not None and event.processing_error == ""


def test_group_ai_task_creation_initializes_persistent_account_scope(session: Session) -> None:
    _seed_scope_base(session)
    session.add(_account(1, 10))
    session.commit()

    task = create_group_ai_chat_task(
        session,
        1,
        GroupAIChatTaskCreate(
            name="新建全部账号任务",
            target_operation_target_id=31,
            hourly_min_messages=10,
        ),
        actor="tester",
    )

    relation = session.scalar(
        select(TaskMembershipAdmissionItem).where(TaskMembershipAdmissionItem.task_id == task.id)
    )
    assert relation is not None
    assert relation.account_id == 1

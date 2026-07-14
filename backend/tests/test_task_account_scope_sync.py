from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    AccountEligibilityEvent,
    AccountPool,
    AccountStatus,
    Action,
    AiAccountVoiceProfile,
    OperationTarget,
    Task,
    TaskAccountDailyCoverage,
    TaskMembershipAdmissionItem,
    Tenant,
    TgAccount,
    TgAccountOnlineState,
    TgGroup,
    TgGroupAccount,
)
from app.integrations.telegram import AccountHealth
from app.security import encrypt_session
from app.schemas.task_center import GroupAIChatTaskCreate
from app.services.task_center.account_scope import (
    eligible_account_ids,
    emit_account_eligibility_event,
    initialize_all_account_task_scope,
    process_account_eligibility_events,
)
from app.services.task_center.channel_membership import _account_can_attempt_membership
from app.services import accounts as accounts_service
from app.services.account_online_state import probe_due_online_states, reconcile_runtime_online_sources
from app.services.task_center.executors import group_ai_chat
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


def test_membership_gate_rejects_unreadable_session_ciphertext() -> None:
    account = TgAccount(
        id=999,
        tenant_id=1,
        display_name="损坏会话",
        phone_masked="999",
        status=AccountStatus.ACTIVE.value,
        session_ciphertext="enc:v2:not-valid-ciphertext",
    )

    assert _account_can_attempt_membership(account) is False


def test_unchanged_health_check_does_not_emit_scope_event(session: Session, monkeypatch) -> None:
    _seed_scope_base(session)
    session.add(_account(1, 10))
    session.commit()
    monkeypatch.setattr(accounts_service, "credentials_for_account", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(
        accounts_service.gateway,
        "check_account_health",
        lambda *_args, **_kwargs: SimpleNamespace(status=AccountStatus.ACTIVE.value, health_score=95, detail="ok"),
    )

    accounts_service.health_check_account(session, 1)

    assert session.scalar(select(AccountEligibilityEvent)) is None


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
    assert legacy_task.type_config["account_coverage_mode"] == "all_accounts_daily"


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


def _seed_new_account_e2e(session: Session) -> Task:
    _seed_scope_base(session)
    task = _task("new-account-e2e")
    task.account_config = {"selection_mode": "all", "cooldown_per_account_minutes": 0}
    task.type_config = {
        **task.type_config,
        "messages_per_round_mode": "manual",
        "messages_per_round": 1,
        "reply_min_per_round": 0,
        "per_account_daily_min_messages": 2,
        "fact_anchor_required": False,
        "low_confidence_silence_enabled": False,
    }
    account = _account(1, 10)
    session.add_all([
        task,
        account,
        TgGroupAccount(tenant_id=1, group_id=21, account_id=1, can_send=True),
        AiAccountVoiceProfile(
            tenant_id=1,
            account_id=1,
            version=1,
            status="active",
            short_prompt_summary="自然短句",
        ),
    ])
    session.commit()
    emit_account_eligibility_event(session, 1, "login_ready")
    session.commit()
    return task


def test_new_account_event_reaches_online_probe_and_planner_action(session: Session, monkeypatch) -> None:
    now = datetime(2026, 7, 13, 22, 30)
    task = _seed_new_account_e2e(session)

    assert process_account_eligibility_events(session, limit=10, now=now) == 1
    relation = session.scalar(select(TaskMembershipAdmissionItem).where(TaskMembershipAdmissionItem.task_id == task.id))
    coverage = session.scalar(select(TaskAccountDailyCoverage).where(TaskAccountDailyCoverage.task_id == task.id))
    assert relation is not None and coverage is not None
    assert reconcile_runtime_online_sources(session, tenant_id=1, include_global=False, now=now) == 1
    state = session.scalar(select(TgAccountOnlineState).where(TgAccountOnlineState.account_id == 1))
    assert state is not None and state.online_status == "warming"

    monkeypatch.setattr(group_ai_chat, "_now", lambda: now)
    monkeypatch.setattr("app.services.task_center.daily_coverage._now", lambda: now)
    assert group_ai_chat.build_plan(session, task) == 0
    assert coverage.blocker_code == "account_offline", (
        task.last_error,
        task.stats,
        coverage.coverage_date,
        coverage.targeted_at,
        coverage.state,
    )
    monkeypatch.setattr("app.services.account_online_probe.credentials_for_account", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(
        "app.services.account_online_probe.gateway.check_account_health",
        lambda *_args: AccountHealth(status=AccountStatus.ACTIVE.value, health_score=96, detail="ok"),
    )
    assert probe_due_online_states(session, limit=10, now=now) == 1
    created = group_ai_chat.build_plan(session, task)
    pending = list(session.scalars(select(Action).where(Action.task_id == task.id, Action.status == "pending")))

    assert state.online_status == "online"
    assert coverage.blocker_code == ""
    assert created == 1
    assert len(pending) == 1
    assert pending[0].payload["ai_generation_status"] == "pending"


def test_failed_scope_event_is_delayed_without_starving_new_events(session: Session, monkeypatch) -> None:
    _seed_scope_base(session)
    session.add_all([_account(1, 10), _account(2, 10)])
    session.commit()
    first = emit_account_eligibility_event(session, 1, "health_status_changed")
    second = emit_account_eligibility_event(session, 2, "login_ready")

    def fail_first(_session, account_id: int, *, now=None):
        if account_id == 1:
            raise RuntimeError("temporary sync failure")
        return 1

    monkeypatch.setattr("app.services.task_center.account_scope.sync_account_to_all_tasks", fail_first)
    timestamp = datetime(2026, 7, 10, 11)

    assert process_account_eligibility_events(session, limit=1, now=timestamp) == 0
    assert first.attempt_count == 1
    assert first.next_attempt_at and first.next_attempt_at > timestamp
    assert process_account_eligibility_events(session, limit=1, now=timestamp) == 1
    assert second.processed_at == timestamp


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

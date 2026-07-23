from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import (
    AccountPool,
    Action,
    ExecutionAttempt,
    OperationTarget,
    Task,
    TaskAccountDailyCoverage,
    TaskMembershipAdmissionItem,
    Tenant,
    TgAccount,
    TgGroup,
    TgGroupAccount,
)
from app.security import encrypt_session
from app.services._common import _now
from app.schemas.task_center import AccountConfig, TaskUpdate
from app.services.task_center.account_scope import drain_account_scope_events, emit_account_eligibility_event
from app.services.task_center.channel_membership import _task_membership_candidates
from app.services.task_center.daily_coverage import backfill_daily_coverage_confirmations, ensure_task_daily_coverage
from app.services.task_center.dispatcher import _sync_all_account_membership_state
from app.services.task_center.service import update_task


pytestmark = pytest.mark.no_postgres
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _engine():
    return create_engine(
        "sqlite://",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


def _seed(session: Session) -> tuple[Task, TgAccount]:
    session.add(Tenant(id=1, name="租户"))
    session.add(AccountPool(id=10, tenant_id=1, name="普通", pool_purpose="normal", is_enabled=True))
    session.add(TgGroup(id=21, tenant_id=1, tg_peer_id="-10021", title="目标群"))
    session.add(OperationTarget(
        id=31,
        tenant_id=1,
        target_type="group",
        tg_peer_id="-10021",
        title="目标群",
        auth_status="已授权运营",
        can_send=True,
    ))
    task = Task(
        id="runtime-coverage",
        tenant_id=1,
        name="运行时覆盖",
        type="group_ai_chat",
        status="running",
        account_config={"selection_mode": "all"},
        type_config={
            "target_group_id": 21,
            "target_operation_target_id": 31,
            "account_coverage_mode": "all_accounts_daily",
        },
    )
    account = TgAccount(
        id=1,
        tenant_id=1,
        pool_id=10,
        display_name="账号1",
        phone_masked="1",
        account_identity="normal",
        status="在线",
        session_ciphertext=encrypt_session("session-1"),
    )
    session.add_all([task, account])
    session.flush()
    return task, account


def test_scope_event_drain_incrementally_adds_account_before_planning() -> None:
    engine = _engine()
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        _task, account = _seed(session)
        emit_account_eligibility_event(session, account.id, "login_ready")
        session.commit()

    assert drain_account_scope_events(factory, limit=10, now=datetime(2026, 7, 10, 10)) == 1

    with factory() as session:
        relation = session.scalar(select(TaskMembershipAdmissionItem))
        assert relation is not None
        assert relation.account_id == 1


def test_membership_gate_reads_persistent_scope_without_platform_scan(monkeypatch) -> None:
    engine = _engine()
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        task, account = _seed(session)
        session.add(TaskMembershipAdmissionItem(
            tenant_id=1,
            task_id=task.id,
            account_id=account.id,
            target_id=31,
            phase="pending",
        ))
        session.commit()
        monkeypatch.setattr(
            "app.services.task_center.channel_membership.candidate_accounts_for_config",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not scan platform accounts")),
        )

        candidates = _task_membership_candidates(session, task)

        assert [item.id for item in candidates] == [1]


def test_membership_failure_remains_visible_as_daily_blocker() -> None:
    engine = _engine()
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        task, account = _seed(session)
        item = TaskMembershipAdmissionItem(
            tenant_id=1,
            task_id=task.id,
            account_id=account.id,
            target_id=31,
            phase="joining",
        )
        session.add(item)
        session.flush()
        row = TaskAccountDailyCoverage(
            id="runtime-row",
            tenant_id=1,
            task_id=task.id,
            group_id=21,
            account_id=account.id,
            membership_item_id=item.id,
            coverage_date=datetime(2026, 7, 10).date(),
            state="pending_admission",
        )
        action = Action(
            id="membership-failed",
            tenant_id=1,
            task_id=task.id,
            task_type=task.type,
            action_type="ensure_target_membership",
            account_id=account.id,
            status="failed",
            payload={"channel_target_id": 31, "channel_id": "-10021", "target_type": "group"},
            result={"error_code": "group_permission_denied", "error_message": "账号受限，无法入群"},
        )
        session.add_all([row, action])
        session.flush()

        _sync_all_account_membership_state(session, action)

        assert item.phase == "failed"
        assert row.state == "blocked"
        assert row.blocker_code == "group_permission_denied"
        assert row.blocker_detail == "账号受限，无法入群"


def test_membership_success_releases_membership_unknown_coverage() -> None:
    engine = _engine()
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        task, account = _seed(session)
        item = TaskMembershipAdmissionItem(
            tenant_id=1,
            task_id=task.id,
            account_id=account.id,
            target_id=31,
            phase="failed",
            failure_type="unknown_after_send",
            manual_required=True,
        )
        session.add(item)
        session.flush()
        row = TaskAccountDailyCoverage(
            id="membership-unknown-row",
            tenant_id=1,
            task_id=task.id,
            group_id=21,
            account_id=account.id,
            membership_item_id=item.id,
            coverage_date=_now().date(),
            state="unknown",
            blocker_code="unknown_after_send",
            blocker_detail="入群结果未知",
        )
        action = Action(
            id="membership-recovered",
            tenant_id=1,
            task_id=task.id,
            task_type=task.type,
            action_type="ensure_target_membership",
            account_id=account.id,
            status="success",
            result={"success": True, "membership_status": "joined"},
        )
        session.add_all([
            row,
            action,
            TgGroupAccount(
                tenant_id=1,
                group_id=21,
                account_id=account.id,
                can_send=True,
            ),
        ])
        session.flush()

        _sync_all_account_membership_state(session, action)

        assert item.phase == "completed"
        assert item.manual_required is False
        assert row.state == "ready"
        assert row.blocker_code == ""
        assert row.blocker_detail == ""


def test_target_admission_retry_success_refreshes_all_account_coverage() -> None:
    engine = _engine()
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        task, account = _seed(session)
        item = TaskMembershipAdmissionItem(
            tenant_id=1,
            task_id=task.id,
            account_id=account.id,
            target_id=31,
            phase="failed",
            failure_type="cannot_send",
        )
        row = TaskAccountDailyCoverage(
            id="target-retry-blocked-row",
            tenant_id=1,
            task_id=task.id,
            group_id=21,
            account_id=account.id,
            membership_item_id=item.id,
            coverage_date=_now().date(),
            state="blocked",
            blocker_code="cannot_send",
            blocker_detail="账号在目标群不可发言",
        )
        retry_task = Task(
            id="target-admission-retry",
            tenant_id=1,
            name="重试目标准入",
            type="target_admission_retry",
            status="running",
            type_config={"target_operation_target_id": 31},
        )
        retry_action = Action(
            id="target-admission-retry-success",
            tenant_id=1,
            task_id=retry_task.id,
            task_type=retry_task.type,
            action_type="ensure_target_membership",
            account_id=account.id,
            status="success",
            payload={"channel_target_id": 31, "channel_id": "-10021", "target_type": "group"},
            result={"success": True, "membership_status": "joined"},
        )
        session.add_all([
            item,
            row,
            retry_task,
            retry_action,
            TgGroupAccount(tenant_id=1, group_id=21, account_id=account.id, can_send=True),
        ])
        session.flush()

        _sync_all_account_membership_state(session, retry_action)

        assert item.phase == "completed"
        assert item.membership_action_id == retry_action.id
        assert row.state == "ready"
        assert row.blocker_code == ""
        assert row.blocker_detail == ""


def test_membership_success_preserves_send_unknown_coverage() -> None:
    engine = _engine()
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        task, account = _seed(session)
        item = TaskMembershipAdmissionItem(
            tenant_id=1,
            task_id=task.id,
            account_id=account.id,
            target_id=31,
            phase="completed",
        )
        send_action = Action(
            id="send-result-unknown",
            tenant_id=1,
            task_id=task.id,
            task_type=task.type,
            action_type="send_message",
            account_id=account.id,
            status="unknown_after_send",
        )
        membership_action = Action(
            id="membership-reconfirmed",
            tenant_id=1,
            task_id=task.id,
            task_type=task.type,
            action_type="ensure_target_membership",
            account_id=account.id,
            status="success",
            result={"success": True, "membership_status": "joined"},
        )
        session.add_all([item, send_action, membership_action])
        session.flush()
        row = TaskAccountDailyCoverage(
            id="send-unknown-row",
            tenant_id=1,
            task_id=task.id,
            group_id=21,
            account_id=account.id,
            membership_item_id=item.id,
            coverage_date=_now().date(),
            state="unknown",
            reserved_action_id=send_action.id,
            blocker_code="unknown_after_send",
        )
        session.add(row)
        session.flush()

        _sync_all_account_membership_state(session, membership_action)

        assert row.state == "unknown"
        assert row.reserved_action_id == send_action.id


def test_listener_fetch_has_explicit_timeout_and_backfill_script_exists() -> None:
    gateway_source = (PROJECT_ROOT / "backend/app/integrations/telegram/gateway.py").read_text()
    config_source = (PROJECT_ROOT / "backend/app/config.py").read_text()

    assert "asyncio.wait_for" in gateway_source
    assert "listener_fetch_timeout_seconds" in config_source
    assert (PROJECT_ROOT / "backend/scripts/reconcile_ai_group_daily_coverage.py").exists()


def test_release_backfill_only_confirms_success_with_remote_message_id() -> None:
    engine = _engine()
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        task, account = _seed(session)
        session.add(TaskMembershipAdmissionItem(
            tenant_id=1,
            task_id=task.id,
            account_id=account.id,
            target_id=31,
            phase="completed",
        ))
        session.flush()
        ensure_task_daily_coverage(
            session,
            task,
            now=datetime(2026, 7, 10, 10),
            account_ids=[account.id],
        )
        action = Action(
            id="backfill-success",
            tenant_id=1,
            task_id=task.id,
            task_type=task.type,
            action_type="send_message",
            account_id=account.id,
            status="success",
            executed_at=datetime(2026, 7, 10, 9),
        )
        session.add(action)
        session.add(ExecutionAttempt(
            tenant_id=1,
            action_id=action.id,
            account_id=account.id,
            attempt_no=1,
            status="success",
            remote_message_id="tg-existing-1",
        ))
        session.flush()

        updated = backfill_daily_coverage_confirmations(session, task, datetime(2026, 7, 10).date())
        row = session.scalar(select(TaskAccountDailyCoverage))

        assert updated == 1
        assert row.state == "confirmed"
        assert row.confirmed_count == 1
        assert row.last_remote_message_id == "tg-existing-1"


def test_release_backfill_never_decreases_existing_confirmation() -> None:
    engine = _engine()
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        task, account = _seed(session)
        session.add(TaskAccountDailyCoverage(
            id="already-confirmed",
            tenant_id=1,
            task_id=task.id,
            group_id=21,
            account_id=account.id,
            coverage_date=datetime(2026, 7, 10).date(),
            target_count=2,
            confirmed_count=2,
            state="confirmed",
            last_success_action_id="retained-success",
            last_remote_message_id="tg-retained",
        ))
        action = Action(
            id="partial-history-success",
            tenant_id=1,
            task_id=task.id,
            task_type=task.type,
            action_type="send_message",
            account_id=account.id,
            status="success",
            executed_at=datetime(2026, 7, 10, 9),
        )
        session.add(action)
        session.add(ExecutionAttempt(
            tenant_id=1,
            action_id=action.id,
            account_id=account.id,
            attempt_no=1,
            status="success",
            remote_message_id="tg-partial",
        ))
        session.flush()

        updated = backfill_daily_coverage_confirmations(session, task, datetime(2026, 7, 10).date())
        row = session.get(TaskAccountDailyCoverage, "already-confirmed")

        assert updated == 0
        assert row.confirmed_count == 2
        assert row.state == "confirmed"
        assert row.last_success_action_id == "retained-success"


def test_account_selection_update_clears_pending_daily_coverage_actions() -> None:
    engine = _engine()
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        task, account = _seed(session)
        session.add(Action(
            id="pending-coverage-action",
            tenant_id=1,
            task_id=task.id,
            task_type=task.type,
            action_type="send_message",
            account_id=account.id,
            status="pending",
            payload={"coverage_ledger_id": "coverage-1"},
        ))
        session.commit()

        update_task(
            session,
            1,
            task.id,
            TaskUpdate(account_config=AccountConfig(selection_mode="manual", account_ids=[account.id])),
            "tester",
        )

        assert session.get(Action, "pending-coverage-action") is None
        assert session.get(Task, task.id).next_run_at is not None

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Action, ExecutionAttempt, Task, TaskAccountDailyCoverage, Tenant, TgAccount, TgGroup
from app.services.task_center.dispatcher import _action_can_reassign, _sync_action_coverage_state


pytestmark = pytest.mark.no_postgres


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as current:
        yield current


def _seed_reserved(session: Session, *, action_status: str = "executing") -> tuple[Action, TaskAccountDailyCoverage]:
    session.add(Tenant(id=1, name="租户"))
    session.add(TgGroup(id=21, tenant_id=1, tg_peer_id="-10021", title="目标群"))
    session.add(TgAccount(id=1, tenant_id=1, display_name="账号1", phone_masked="1", status="在线"))
    task = Task(id="coverage-dispatch", tenant_id=1, name="覆盖发送", type="group_ai_chat", status="running")
    action = Action(
        id="coverage-action",
        tenant_id=1,
        task_id=task.id,
        task_type=task.type,
        action_type="send_message",
        account_id=1,
        status=action_status,
        payload={
            "group_id": 21,
            "message_text": "自然生成内容",
            "coverage_ledger_id": "coverage-row",
            "account_coverage_mode": "all_accounts_daily",
        },
    )
    row = TaskAccountDailyCoverage(
        id="coverage-row",
        tenant_id=1,
        task_id=task.id,
        group_id=21,
        account_id=1,
        coverage_date=date.today(),
        target_count=1,
        confirmed_count=0,
        state="reserved",
        reserved_action_id=action.id,
    )
    session.add_all([task, action, row])
    session.commit()
    return action, row


def test_success_requires_successful_attempt_with_remote_message_id(session: Session) -> None:
    action, row = _seed_reserved(session)
    action.status = "success"
    session.add(ExecutionAttempt(
        tenant_id=1,
        action_id=action.id,
        account_id=1,
        attempt_no=1,
        status="success",
        remote_message_id="tg-1001",
    ))
    session.flush()

    _sync_action_coverage_state(session, action)
    _sync_action_coverage_state(session, action)
    session.flush()

    assert row.state == "confirmed"
    assert row.confirmed_count == 1
    assert row.last_success_action_id == action.id
    assert row.last_remote_message_id == "tg-1001"


def test_success_without_remote_message_id_stays_unknown_and_unconfirmed(session: Session) -> None:
    action, row = _seed_reserved(session)
    action.status = "success"
    session.add(ExecutionAttempt(
        tenant_id=1,
        action_id=action.id,
        account_id=1,
        attempt_no=1,
        status="success",
        remote_message_id="",
    ))
    session.flush()

    _sync_action_coverage_state(session, action)

    assert row.state == "unknown"
    assert row.confirmed_count == 0
    assert row.blocker_code == "remote_message_id_missing"


@pytest.mark.parametrize("status", ["failed", "skipped", "retryable_failed"])
def test_terminal_preconfirmation_failure_releases_obligation(session: Session, status: str) -> None:
    action, row = _seed_reserved(session)
    action.status = status
    action.result = {"error_code": "duplicate_message", "error_message": "重复内容"}

    _sync_action_coverage_state(session, action)

    assert row.state == "ready"
    assert row.reserved_action_id is None
    assert row.confirmed_count == 0
    assert row.blocker_code == "duplicate_message"


def test_retryable_coverage_failure_requires_fresh_planning(session: Session) -> None:
    action, row = _seed_reserved(session)
    action.status = "retryable_failed"
    action.result = {"error_code": "telegram_timeout", "error_message": "发送超时"}

    _sync_action_coverage_state(session, action)

    assert row.state == "ready"
    assert action.status == "failed"
    assert action.result["coverage_replan_required"] is True


def test_unknown_after_send_keeps_obligation_out_of_immediate_retry(session: Session) -> None:
    action, row = _seed_reserved(session)
    action.status = "unknown_after_send"
    action.result = {"error_code": "unknown_after_send", "error_message": "连接中断"}

    _sync_action_coverage_state(session, action)

    assert row.state == "unknown"
    assert row.reserved_action_id == action.id
    assert row.confirmed_count == 0
    assert row.blocker_code == "unknown_after_send"


def test_coverage_action_cannot_be_reassigned_to_another_account(session: Session) -> None:
    action, _row = _seed_reserved(session)

    assert _action_can_reassign(action) is False

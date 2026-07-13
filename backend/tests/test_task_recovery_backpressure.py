from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.database import Base
from app.integrations.telegram import OperationResult
from app.models import Action, ExecutionAttempt, OperationTarget, Tenant, TgAccount, Task
from app.services._common import _now
from app.services.task_center import service as task_service
from app.services.task_center.service import _recover_stale_executing_actions, drain_task_recovery


pytestmark = pytest.mark.no_postgres


@pytest.mark.allow_missing_rule_binding
def test_recovery_preserves_lifetime_cap_comment_completion_and_recovers_regular_dynamic_task():
    completion_stats = {
        "completion_reason": "lifetime_cap_reached",
        "completed_at": "2026-07-13T16:51:56.362702",
        "max_total_comments_resolved": 86,
        "remote_success_count": 51,
        "unknown_after_send_count": 35,
    }
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        lifetime_task = Task(
            id="lifetime-cap-comment", tenant_id=1, name="生命周期已完成评论",
            type="channel_comment", status="completed", next_run_at=None, scheduled_end=None,
            type_config={"message_scope": "dynamic_new"}, stats=completion_stats,
        )
        regular_task = Task(
            id="regular-dynamic-comment", tenant_id=1, name="普通动态评论",
            type="channel_comment", status="completed", next_run_at=None, scheduled_end=None,
            type_config={"message_scope": "dynamic_new"}, stats={},
        )
        session.add_all([lifetime_task, regular_task])
        session.flush()

        recovered = task_service._recover_continuous_task_states(session)

    assert recovered == 1
    assert lifetime_task.status == "completed"
    assert lifetime_task.next_run_at is None
    assert lifetime_task.stats == completion_stats
    assert regular_task.status == "running"
    assert regular_task.next_run_at is not None


@pytest.mark.allow_missing_rule_binding
def test_recovery_recovers_regular_dynamic_task_with_non_mapping_stats():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        task = Task(
            id="legacy-stats-dynamic-comment",
            tenant_id=1,
            name="历史统计动态评论",
            type="channel_comment",
            status="completed",
            next_run_at=None,
            scheduled_end=None,
            type_config={"message_scope": "dynamic_new"},
            stats=["legacy-stats"],
        )
        session.add(task)
        session.flush()

        recovered = task_service._recover_continuous_task_states(session)

    assert recovered == 1
    assert task.status == "running"
    assert task.next_run_at is not None
    assert task.stats == ["legacy-stats"]


def test_recovery_limits_unknown_membership_reprobes_to_drain_limit(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    SessionFactory = sessionmaker(bind=engine, future=True)
    calls: list[str] = []

    with SessionFactory() as session:
        _seed_unknown_membership_actions(session, count=4)

    def fake_probe(_account_id, target_peer_id, *_args, **_kwargs):
        calls.append(str(target_peer_id))
        return OperationResult(False, detail="仍不可访问")

    monkeypatch.setattr(task_service, "credentials_for_account", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(task_service.gateway, "probe_target_capabilities", fake_probe)

    assert drain_task_recovery(SessionFactory, limit=2) >= 0

    assert calls == ["@target_3", "@target_2"]


def test_recovery_records_unknown_membership_probe_timeout(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()

    with Session(engine) as session:
        _seed_unknown_membership_actions(session, count=1, now_value=now_value)

        monkeypatch.setattr(task_service, "credentials_for_account", lambda *_args, **_kwargs: object())
        monkeypatch.setattr(task_service.gateway, "probe_target_capabilities", _raise_timeout)

        assert _recover_stale_executing_actions(session, timeout_minutes=30) == 0

        action = session.get(Action, "action-membership-0")
        assert action.status == "unknown_after_send"
        assert action.result["unknown_membership_reprobe_status"] == "timeout"
        assert action.result["error_code"] == "telegram_probe_timeout"
        assert action.result["unknown_membership_reprobe_next_at"] > now_value.isoformat()


def test_recovery_skips_failed_reprobe_rows_when_selecting_batch(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    SessionFactory = sessionmaker(bind=engine, future=True)
    calls: list[str] = []

    with SessionFactory() as session:
        _seed_unknown_membership_actions(session, count=3)
        now_value = _now()
        for index in range(3):
            action = session.get(Action, f"action-membership-{index}")
            action.result = {"error_code": "unknown_after_send", "unknown_membership_reprobe_status": "failed"}
        session.add(OperationTarget(id=799, tenant_id=1, title="目标 99", target_type="group", tg_peer_id="@target_99"))
        session.add(TgAccount(id=1199, tenant_id=1, display_name="账号 99", phone_masked="+861***1199", status="在线", session_ciphertext="session"))
        due_action = _unknown_membership_action(99, now_value)
        due_action.id = "action-membership-due"
        due_action.executed_at = now_value
        session.add(due_action)
        session.commit()

    def fake_probe(_account_id, target_peer_id, *_args, **_kwargs):
        calls.append(str(target_peer_id))
        return OperationResult(False, detail="仍不可访问")

    monkeypatch.setattr(task_service, "credentials_for_account", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(task_service.gateway, "probe_target_capabilities", fake_probe)

    assert drain_task_recovery(SessionFactory, limit=2) >= 0

    assert calls == ["@target_99"]


def test_recovery_marks_failed_probe_result_and_skips_next_round(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    SessionFactory = sessionmaker(bind=engine, future=True)
    calls: list[str] = []

    with SessionFactory() as session:
        _seed_unknown_membership_actions(session, count=1)

    def fake_probe(_account_id, target_peer_id, *_args, **_kwargs):
        calls.append(str(target_peer_id))
        return OperationResult(False, "失败", "unknown_after_send", "Server closed the connection")

    monkeypatch.setattr(task_service, "credentials_for_account", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(task_service.gateway, "probe_target_capabilities", fake_probe)

    assert drain_task_recovery(SessionFactory, limit=1) >= 0
    assert drain_task_recovery(SessionFactory, limit=1) >= 0

    with SessionFactory() as session:
        action = session.get(Action, "action-membership-0")
        assert action.result["unknown_membership_reprobe_status"] == "failed"
        assert action.result["error_code"] == "unknown_after_send"
        assert action.result["unknown_membership_reprobe_error"] == "Server closed the connection"

    assert calls == ["@target_0"]


def test_recovery_marks_duplicate_identity_probe_rows_failed(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    SessionFactory = sessionmaker(bind=engine, future=True)
    calls: list[str] = []

    with SessionFactory() as session:
        now_value = _now()
        _seed_unknown_membership_actions(session, count=1, now_value=now_value)
        for index in range(2):
            duplicate = _unknown_membership_action(0, now_value - timedelta(minutes=index + 1))
            duplicate.id = f"action-membership-duplicate-{index}"
            duplicate.executed_at = now_value - timedelta(minutes=20 + index)
            session.add(duplicate)
        session.commit()

    def fake_probe(_account_id, target_peer_id, *_args, **_kwargs):
        calls.append(str(target_peer_id))
        return OperationResult(False, "失败", "unknown_after_send", "Server closed the connection")

    monkeypatch.setattr(task_service, "credentials_for_account", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(task_service.gateway, "probe_target_capabilities", fake_probe)

    assert drain_task_recovery(SessionFactory, limit=3) >= 0
    assert drain_task_recovery(SessionFactory, limit=3) >= 0

    with SessionFactory() as session:
        for action_id in [
            "action-membership-0",
            "action-membership-duplicate-0",
            "action-membership-duplicate-1",
        ]:
            action = session.get(Action, action_id)
            assert action.result["unknown_membership_reprobe_status"] == "failed"
            assert action.result["unknown_membership_reprobe_error"] == "Server closed the connection"

    assert calls == ["@target_0"]


def test_stale_executing_membership_timeout_clears_lease_and_cools_down(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()
    calls: list[str] = []

    with Session(engine) as session:
        _seed_stale_executing_membership_action(session, now_value=now_value)

        def fake_probe(_account_id, target_peer_id, *_args, **_kwargs):
            calls.append(str(target_peer_id))
            raise TimeoutError("telegram probe timed out")

        monkeypatch.setattr(task_service, "credentials_for_account", lambda *_args, **_kwargs: object())
        monkeypatch.setattr(task_service.gateway, "probe_target_capabilities", fake_probe)

        assert _recover_stale_executing_actions(session, timeout_minutes=30, limit=1) == 0

        action = session.get(Action, "action-executing-membership")
        assert action.status == "unknown_after_send"
        assert action.lease_owner == ""
        assert action.lease_expires_at is None
        assert action.result["error_code"] == "telegram_probe_timeout"
        assert action.result["unknown_membership_reprobe_status"] == "timeout"

        assert _recover_stale_executing_actions(session, timeout_minutes=30, limit=1) == 0

    assert calls == ["@stale_target"]


def test_stale_executing_membership_failed_probe_clears_lease_and_stops_reprobe(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()
    calls: list[str] = []

    with Session(engine) as session:
        _seed_stale_executing_membership_action(session, now_value=now_value)

        def fake_probe(_account_id, target_peer_id, *_args, **_kwargs):
            calls.append(str(target_peer_id))
            return OperationResult(False, "失败", "unknown_after_send", "Server closed the connection")

        monkeypatch.setattr(task_service, "credentials_for_account", lambda *_args, **_kwargs: object())
        monkeypatch.setattr(task_service.gateway, "probe_target_capabilities", fake_probe)

        assert _recover_stale_executing_actions(session, timeout_minutes=30, limit=1) >= 0

        action = session.get(Action, "action-executing-membership")
        assert action.status == "unknown_after_send"
        assert action.lease_owner == ""
        assert action.lease_expires_at is None
        assert action.result["error_code"] == "unknown_after_send"
        assert action.result["unknown_membership_reprobe_status"] == "failed"
        assert action.result["unknown_membership_reprobe_error"] == "Server closed the connection"

        assert _recover_stale_executing_actions(session, timeout_minutes=30, limit=1) == 0

    assert calls == ["@stale_target"]


def test_stale_executing_membership_connection_error_clears_lease_and_cools_down(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()
    calls: list[str] = []

    with Session(engine) as session:
        _seed_stale_executing_membership_action(session, now_value=now_value)

        def fake_probe(_account_id, target_peer_id, *_args, **_kwargs):
            calls.append(str(target_peer_id))
            raise ConnectionError("Connection to Telegram failed 5 time(s)")

        monkeypatch.setattr(task_service, "credentials_for_account", lambda *_args, **_kwargs: object())
        monkeypatch.setattr(task_service.gateway, "probe_target_capabilities", fake_probe)

        assert _recover_stale_executing_actions(session, timeout_minutes=30, limit=1) == 0

        action = session.get(Action, "action-executing-membership")
        assert action.status == "unknown_after_send"
        assert action.lease_owner == ""
        assert action.lease_expires_at is None
        assert action.result["error_code"] == "telegram_probe_connection_error"
        assert action.result["unknown_membership_reprobe_status"] == "connection_error"

        assert _recover_stale_executing_actions(session, timeout_minutes=30, limit=1) == 0

    assert calls == ["@stale_target"]


def _raise_timeout(*_args, **_kwargs):
    raise TimeoutError("telegram probe timed out")


def _seed_unknown_membership_actions(session: Session, *, count: int, now_value=None) -> None:
    now = now_value or _now()
    session.add(Tenant(id=1, name="默认运营空间"))
    session.add(Task(id="task-membership", tenant_id=1, name="retry", type="target_admission_retry", status="running", stats={}))
    for index in range(count):
        session.add(OperationTarget(id=700 + index, tenant_id=1, title=f"目标 {index}", target_type="group", tg_peer_id=f"@target_{index}"))
        session.add(TgAccount(id=1100 + index, tenant_id=1, display_name=f"账号 {index}", phone_masked=f"+861***{index:04d}", status="在线", session_ciphertext="session"))
        session.add(_unknown_membership_action(index, now))
    session.commit()


def _unknown_membership_action(index: int, now_value) -> Action:
    return Action(
        id=f"action-membership-{index}",
        tenant_id=1,
        task_id="task-membership",
        task_type="target_admission_retry",
        action_type="ensure_target_membership",
        account_id=1100 + index,
        status="unknown_after_send",
        scheduled_at=now_value - timedelta(hours=1, minutes=index),
        executed_at=now_value - timedelta(minutes=10 + index),
        payload={
            "channel_id": f"@target_{index}",
            "channel_target_id": 700 + index,
            "target_type": "group",
            "require_send": True,
        },
        result={"error_code": "unknown_after_send"},
    )


def _seed_stale_executing_membership_action(session: Session, *, now_value) -> None:
    session.add(Tenant(id=1, name="默认运营空间"))
    session.add(OperationTarget(id=901, tenant_id=1, title="stale target", target_type="group", tg_peer_id="@stale_target"))
    session.add(TgAccount(id=1901, tenant_id=1, display_name="执行账号", phone_masked="+861***1901", status="在线", session_ciphertext="session"))
    session.add(Task(id="task-executing-membership", tenant_id=1, name="executing", type="target_admission_retry", status="running", stats={}))
    session.add(
        Action(
            id="action-executing-membership",
            tenant_id=1,
            task_id="task-executing-membership",
            task_type="target_admission_retry",
            action_type="ensure_target_membership",
            account_id=1901,
            status="executing",
            lease_owner="worker-a",
            lease_expires_at=now_value - timedelta(minutes=1),
            scheduled_at=now_value - timedelta(hours=1),
            payload={"channel_id": "@stale_target", "channel_target_id": 901, "target_type": "group", "require_send": True},
            result={},
        )
    )
    session.add(
        ExecutionAttempt(
            id="attempt-executing-membership",
            tenant_id=1,
            action_id="action-executing-membership",
            worker_id="worker-a",
            attempt_no=1,
            status="gateway_call_started",
            gateway_call_started_at=now_value - timedelta(minutes=5),
        )
    )
    session.commit()

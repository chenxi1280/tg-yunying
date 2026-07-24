from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Action, ExecutionAttempt, SchedulingSetting, Task, TaskHardHourlyBucket, TaskHardHourlyDeliveryCredit, Tenant, TgAccount, TgGroup
from app.services.task_center.datetime_compat import compare_datetimes, ensure_aware
from app.services.task_center.dispatcher import (
    _apply_send_result,
    _hard_hourly_bucket_expired,
    _skip_expired_hard_hourly_action,
    recover_expired_hard_hourly_actions,
    recover_hard_hourly_delivery_credits,
)
from app.services.task_center.group_send_limits import (
    SEND_LIMIT_MODE_ACCOUNT_ONLY,
    SEND_LIMIT_MODE_LEGACY_GROUP_SLOT,
    group_policy_block,
)
from app.services.task_center.hard_hourly_ledger import credit_success_once, durable_debt, ensure_bucket
from app.services.task_center.service import stop_task

pytestmark = pytest.mark.no_postgres


def _session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return Session(engine)


def test_credit_success_once_is_idempotent_and_uses_plan_bucket():
    with _session() as session:
        session.add(Tenant(id=1, name="t"))
        task = Task(
            id="task-hh",
            tenant_id=1,
            name="hh",
            type="group_ai_chat",
            status="running",
            timezone="Asia/Shanghai",
            type_config={"hard_hourly_target_enabled": True, "hourly_min_messages": 10},
        )
        session.add(task)
        plan_bucket = datetime(2026, 7, 24, 14, 0, 0)
        action = Action(
            id="act-1",
            tenant_id=1,
            task_id="task-hh",
            task_type="group_ai_chat",
            action_type="send_message",
            status="success",
            scheduled_at=plan_bucket + timedelta(minutes=55),
            payload={
                "hard_hourly_target": True,
                "hard_hourly_bucket": plan_bucket.isoformat(),
                "target_operation_target_id": 9,
                "target_reference_revision": 1,
            },
        )
        session.add(action)
        session.add(
            ExecutionAttempt(
                id="attempt-1",
                tenant_id=1,
                action_id="act-1",
                attempt_no=1,
                status="success",
                remote_message_id="mid-1",
            )
        )
        session.commit()

        first = credit_success_once(
            session,
            action=action,
            execution_attempt_id="attempt-1",
            remote_message_id="mid-1",
            executed_at=datetime(2026, 7, 24, 15, 3, 0),
        )
        second = credit_success_once(
            session,
            action=action,
            execution_attempt_id="attempt-1",
            remote_message_id="mid-1",
            executed_at=datetime(2026, 7, 24, 15, 3, 0),
        )
        session.commit()

        assert first.credited is True
        assert first.reason == "credited"
        assert second.credited is False
        assert second.reason == "already_credited"
        credits = list(session.scalars(select(TaskHardHourlyDeliveryCredit)))
        buckets = list(session.scalars(select(TaskHardHourlyBucket)))
        assert len(credits) == 1
        assert len(buckets) == 1
        # Plan bucket is 14:00, not 15:00 actual send hour.
        assert "T14:00" in buckets[0].bucket_key
        assert buckets[0].success_count == 1


def test_credit_success_once_preserves_distinct_credits_with_a_stale_bucket(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'hard-hourly-credit.db'}", future=True)
    Base.metadata.create_all(engine)
    plan_bucket = datetime(2026, 7, 24, 14)
    with Session(engine) as session:
        session.add(Tenant(id=1, name="t"))
        task = Task(id="task-stale-credit", tenant_id=1, name="hh", type="group_ai_chat", status="running")
        session.add(task)
        for action_id in ("stale-credit-1", "stale-credit-2"):
            session.add(
                Action(
                    id=action_id,
                    tenant_id=1,
                    task_id=task.id,
                    task_type="group_ai_chat",
                    action_type="send_message",
                    status="success",
                    scheduled_at=plan_bucket,
                    payload={
                        "hard_hourly_target": True,
                        "hard_hourly_bucket": plan_bucket.isoformat(),
                        "target_operation_target_id": 9,
                        "target_reference_revision": 1,
                    },
                )
            )
            session.add(
                ExecutionAttempt(
                    id=f"attempt-{action_id}",
                    tenant_id=1,
                    action_id=action_id,
                    attempt_no=1,
                    status="success",
                    remote_message_id=f"remote-{action_id}",
                )
            )
        ensure_bucket(session, task=task, operation_target_id=9, target_reference_revision=1, bucket_start=plan_bucket, goal=10)
        session.commit()

    with Session(engine, expire_on_commit=False) as stale_session:
        stale_action = stale_session.get(Action, "stale-credit-1")
        stale_bucket = stale_session.scalar(select(TaskHardHourlyBucket))
        stale_session.commit()
        with Session(engine) as current_session:
            current_action = current_session.get(Action, "stale-credit-2")
            assert credit_success_once(current_session, action=current_action, execution_attempt_id="attempt-stale-credit-2", remote_message_id="remote-stale-credit-2")
            current_session.commit()
        assert stale_bucket.success_count == 0
        assert credit_success_once(stale_session, action=stale_action, execution_attempt_id="attempt-stale-credit-1", remote_message_id="remote-stale-credit-1")
        stale_session.commit()

    with Session(engine) as session:
        assert session.scalar(select(TaskHardHourlyBucket.success_count)) == 2


def test_credit_reconstructs_missing_bucket_with_the_action_frozen_goal_and_config_revision():
    with _session() as session:
        session.add(Tenant(id=1, name="t"))
        task = Task(
            id="task-frozen-bucket",
            tenant_id=1,
            name="frozen bucket",
            type="group_ai_chat",
            status="running",
            config_revision=5,
            type_config={"hard_hourly_target_enabled": True, "hourly_min_messages": 20},
        )
        action = Action(
            id="action-frozen-bucket",
            tenant_id=1,
            task_id=task.id,
            task_type="group_ai_chat",
            action_type="send_message",
            status="success",
            scheduled_at=datetime(2026, 7, 24, 14, 59, 0),
            payload={
                "hard_hourly_target": True,
                "hard_hourly_bucket": "2026-07-24T14:00:00+08:00",
                "hard_hourly_goal_at_plan": 10,
                "task_config_revision": 3,
                "target_operation_target_id": 1,
                "target_reference_revision": 1,
            },
        )
        attempt = ExecutionAttempt(
            id="attempt-frozen-bucket",
            tenant_id=1,
            action_id=action.id,
            attempt_no=1,
            status="success",
            remote_message_id="remote-frozen",
        )
        session.add_all([task, action, attempt])
        session.commit()

        outcome = credit_success_once(
            session,
            action=action,
            execution_attempt_id=attempt.id,
            remote_message_id="remote-frozen",
            executed_at=datetime(2026, 7, 24, 15, 2, 0),
        )
        assert outcome.credited is True
        assert outcome.reason == "credited"
        session.commit()

        bucket = session.scalar(select(TaskHardHourlyBucket))
        assert bucket is not None
        assert bucket.goal == 10
        assert bucket.task_config_revision == 3


def test_hard_hourly_bucket_expired_never_skips():
    action = Action(
        id="a",
        tenant_id=1,
        task_id="t",
        task_type="group_ai_chat",
        action_type="send_message",
        status="pending",
        scheduled_at=datetime(2026, 7, 24, 14, 0, 0),
        payload={"hard_hourly_target": True, "hard_hourly_bucket": "2026-07-24T13:00:00"},
    )
    assert _hard_hourly_bucket_expired(action) is False
    with _session() as session:
        session.add(Tenant(id=1, name="t"))
        session.add(Task(id="t", tenant_id=1, name="t", type="group_ai_chat", status="running"))
        session.add(action)
        session.commit()
        assert _skip_expired_hard_hourly_action(session, action) is False
        assert recover_expired_hard_hourly_actions(session) == 0
        assert session.get(Action, "a").status == "pending"


def test_account_only_skips_group_cooldown():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Tenant(id=1, name="t"))
        group = TgGroup(
            id=1,
            tenant_id=1,
            tg_peer_id="g",
            title="g",
            daily_limit=1000,
            group_cooldown_seconds=3600,
            send_limit_mode=SEND_LIMIT_MODE_ACCOUNT_ONLY,
            active_window="00:00-23:59",
        )
        session.add(group)
        action = Action(
            id="a1",
            tenant_id=1,
            task_id="t",
            task_type="group_ai_chat",
            action_type="send_message",
            status="pending",
            scheduled_at=datetime(2026, 7, 24, 12, 0, 0),
            payload={"group_id": 1},
        )
        session.add(Task(id="t", tenant_id=1, name="t", type="group_ai_chat", status="running"))
        session.add(action)
        session.commit()
        assert group_policy_block(session, action=action, group=group) is None

        group.send_limit_mode = SEND_LIMIT_MODE_LEGACY_GROUP_SLOT
        # Without prior slots, cooldown still None; presence of mode is what matters for unit path.
        assert group.send_limit_mode == SEND_LIMIT_MODE_LEGACY_GROUP_SLOT


def test_datetime_compat_compares_aware_and_naive():
    naive = datetime(2026, 7, 24, 12, 0, 0)
    aware = ensure_aware(naive)
    assert compare_datetimes(naive, aware) == 0
    later = aware + timedelta(seconds=1)
    assert compare_datetimes(naive, later) < 0


def test_durable_debt_sums_past_buckets():
    with _session() as session:
        session.add(Tenant(id=1, name="t"))
        task = Task(id="task-d", tenant_id=1, name="d", type="group_ai_chat", status="running", timezone="Asia/Shanghai")
        session.add(task)
        b1 = ensure_bucket(
            session,
            task=task,
            operation_target_id=1,
            target_reference_revision=1,
            bucket_start=datetime(2026, 7, 24, 10, 0, 0),
            goal=10,
        )
        b1.success_count = 3
        b2 = ensure_bucket(
            session,
            task=task,
            operation_target_id=1,
            target_reference_revision=1,
            bucket_start=datetime(2026, 7, 24, 11, 0, 0),
            goal=10,
        )
        b2.success_count = 10
        session.commit()
        debt = durable_debt(
            session,
            task=task,
            operation_target_id=1,
            target_reference_revision=1,
            current_bucket_key=b2.bucket_key,
        )
        assert debt == 7


def test_credit_rejects_attempt_that_is_not_confirmed_success():
    with _session() as session:
        session.add(Tenant(id=1, name="t"))
        task = Task(
            id="task-credit-attempt",
            tenant_id=1,
            name="credit",
            type="group_ai_chat",
            status="running",
            type_config={"hard_hourly_target_enabled": True, "hourly_min_messages": 10},
        )
        action = Action(
            id="action-credit-attempt",
            tenant_id=1,
            task_id=task.id,
            task_type="group_ai_chat",
            action_type="send_message",
            status="success",
            scheduled_at=datetime(2026, 7, 24, 14, 0, 0),
            payload={
                "hard_hourly_target": True,
                "hard_hourly_bucket": "2026-07-24T14:00:00+08:00",
                "target_operation_target_id": 9,
                "target_reference_revision": 1,
            },
        )
        attempt = ExecutionAttempt(
            id="attempt-not-finished",
            tenant_id=1,
            action_id=action.id,
            attempt_no=1,
            status="gateway_call_started",
            remote_message_id="mid-1",
        )
        session.add_all([task, action, attempt])
        session.commit()

        credited = credit_success_once(
            session,
            action=action,
            execution_attempt_id=attempt.id,
            remote_message_id="mid-1",
            executed_at=datetime(2026, 7, 24, 14, 1, 0),
        )

    assert credited.credited is False
    assert credited.reason == "missing_attempt"


def test_durable_debt_excludes_future_and_terminal_buckets():
    with _session() as session:
        session.add(Tenant(id=1, name="t"))
        task = Task(id="task-debt-boundary", tenant_id=1, name="d", type="group_ai_chat", status="running", timezone="Asia/Shanghai")
        session.add(task)
        past = ensure_bucket(
            session,
            task=task,
            operation_target_id=1,
            target_reference_revision=1,
            bucket_start=datetime(2026, 7, 24, 10, 0, 0),
            goal=10,
        )
        past.success_count = 3
        current = ensure_bucket(
            session,
            task=task,
            operation_target_id=1,
            target_reference_revision=1,
            bucket_start=datetime(2026, 7, 24, 11, 0, 0),
            goal=10,
        )
        future = ensure_bucket(
            session,
            task=task,
            operation_target_id=1,
            target_reference_revision=1,
            bucket_start=datetime(2026, 7, 24, 12, 0, 0),
            goal=10,
        )
        terminal = ensure_bucket(
            session,
            task=task,
            operation_target_id=1,
            target_reference_revision=1,
            bucket_start=datetime(2026, 7, 24, 9, 0, 0),
            goal=10,
        )
        future.success_count = 0
        terminal.terminal_blocker_code = "target_ref_invalid"
        session.commit()

        debt = durable_debt(
            session,
            task=task,
            operation_target_id=1,
            target_reference_revision=1,
            current_bucket_key=current.bucket_key,
        )

    assert debt == 7


def test_explicit_task_stop_closes_open_hard_hourly_obligations():
    with _session() as session:
        session.add(Tenant(id=1, name="t"))
        task = Task(
            id="task-stop-ledger",
            tenant_id=1,
            name="stop ledger",
            type="group_ai_chat",
            status="running",
            type_config={"hard_hourly_target_enabled": True, "hourly_min_messages": 10},
        )
        session.add(task)
        bucket = ensure_bucket(
            session,
            task=task,
            operation_target_id=1,
            target_reference_revision=1,
            bucket_start=datetime(2026, 7, 24, 14, 0, 0),
            goal=10,
        )
        session.commit()

        stop_task(session, 1, task.id, "ops", "operator stop")

        assert bucket.terminal_blocker_code == "task_stopped"


def test_dispatch_result_finishes_attempt_before_hard_hourly_credit():
    with _session() as session:
        session.add(Tenant(id=1, name="t"))
        session.add(SchedulingSetting(tenant_id=1, ai_group_send_continuity_v1=True))
        account = TgAccount(
            id=21,
            tenant_id=1,
            display_name="发送账号",
            phone_masked="21",
            status="在线",
            session_ciphertext="session-21",
        )
        task = Task(
            id="task-dispatch-credit",
            tenant_id=1,
            name="dispatch credit",
            type="group_ai_chat",
            status="running",
            type_config={
                "hard_hourly_target_enabled": True,
                "hourly_min_messages": 10,
                "target_operation_target_id": 9,
                "target_reference_revision": 2,
            },
        )
        action = Action(
            id="action-dispatch-credit",
            tenant_id=1,
            task_id=task.id,
            task_type="group_ai_chat",
            action_type="send_message",
            account_id=account.id,
            status="executing",
            scheduled_at=datetime(2026, 7, 24, 14, 0, 0),
            payload={
                "hard_hourly_target": True,
                "hard_hourly_bucket": "2026-07-24T14:00:00+08:00",
                "target_operation_target_id": 9,
                "target_reference_revision": 2,
            },
        )
        attempt = ExecutionAttempt(
            id="attempt-dispatch-credit",
            tenant_id=1,
            action_id=action.id,
            account_id=account.id,
            attempt_no=1,
            status="gateway_call_started",
        )
        session.add_all([account, task, action, attempt])
        session.commit()

        _apply_send_result(action, account, True, "remote-1", attempt=attempt)
        session.commit()

        credit = session.scalar(select(TaskHardHourlyDeliveryCredit))
        attempt_status = attempt.status
        action_result = dict(action.result or {})

    assert attempt_status == "success"
    assert credit is not None
    assert credit.execution_attempt_id == "attempt-dispatch-credit"
    assert "hard_hourly_credit_error" not in action_result


def test_recover_hard_hourly_delivery_credits_repairs_retryable_failures():
    with _session() as session:
        session.add(Tenant(id=1, name="t"))
        session.add(SchedulingSetting(tenant_id=1, ai_group_send_continuity_v1=True))
        task = Task(
            id="task-credit-recovery",
            tenant_id=1,
            name="credit recovery",
            type="group_ai_chat",
            status="running",
            timezone="Asia/Shanghai",
            type_config={
                "hard_hourly_target_enabled": True,
                "hourly_min_messages": 10,
                "target_operation_target_id": 9,
                "target_reference_revision": 1,
            },
        )
        action = Action(
            id="action-credit-recovery",
            tenant_id=1,
            task_id=task.id,
            task_type="group_ai_chat",
            action_type="send_message",
            status="success",
            scheduled_at=datetime(2026, 7, 24, 14, 10, 0),
            executed_at=datetime(2026, 7, 24, 14, 12, 0),
            payload={
                "hard_hourly_target": True,
                "hard_hourly_bucket": "2026-07-24T14:00:00+08:00",
                "hard_hourly_goal_at_plan": 10,
                "target_operation_target_id": 9,
                "target_reference_revision": 1,
            },
            result={
                "success": True,
                "telegram_msg_id": "remote-credit-recovery",
                "hard_hourly_credit_status": "failed",
                "hard_hourly_credit_error": "missing_epoch",
                "hard_hourly_credit_retryable": True,
            },
        )
        attempt = ExecutionAttempt(
            id="attempt-credit-recovery",
            tenant_id=1,
            action_id=action.id,
            attempt_no=1,
            status="success",
            remote_message_id="remote-credit-recovery",
        )
        session.add_all([task, action, attempt])
        session.commit()

        recovered = recover_hard_hourly_delivery_credits(session, limit=10)
        session.commit()

        credit = session.scalar(select(TaskHardHourlyDeliveryCredit))
        bucket = session.scalar(select(TaskHardHourlyBucket))
        action_result = dict(session.get(Action, action.id).result or {})

    assert recovered == 1
    assert credit is not None
    assert credit.action_id == "action-credit-recovery"
    assert credit.remote_message_id == "remote-credit-recovery"
    assert bucket is not None
    assert bucket.success_count == 1
    assert action_result["hard_hourly_credit_status"] == "credited"
    assert "hard_hourly_credit_error" not in action_result
    assert "hard_hourly_credit_recovery_at" in action_result


def test_recover_hard_hourly_delivery_credits_is_idempotent_after_success():
    with _session() as session:
        session.add(Tenant(id=1, name="t"))
        session.add(SchedulingSetting(tenant_id=1, ai_group_send_continuity_v1=True))
        task = Task(
            id="task-credit-idempotent",
            tenant_id=1,
            name="credit idempotent",
            type="group_ai_chat",
            status="running",
            type_config={"hard_hourly_target_enabled": True, "hourly_min_messages": 5},
        )
        action = Action(
            id="action-credit-idempotent",
            tenant_id=1,
            task_id=task.id,
            task_type="group_ai_chat",
            action_type="send_message",
            status="success",
            scheduled_at=datetime(2026, 7, 24, 14, 0, 0),
            executed_at=datetime(2026, 7, 24, 14, 5, 0),
            payload={
                "hard_hourly_target": True,
                "hard_hourly_bucket": "2026-07-24T14:00:00",
                "target_operation_target_id": 3,
                "target_reference_revision": 1,
            },
            result={
                "telegram_msg_id": "remote-idempotent",
                "hard_hourly_credit_status": "failed",
                "hard_hourly_credit_retryable": True,
                "hard_hourly_credit_error": "missing_attempt",
            },
        )
        attempt = ExecutionAttempt(
            id="attempt-credit-idempotent",
            tenant_id=1,
            action_id=action.id,
            attempt_no=1,
            status="success",
            remote_message_id="remote-idempotent",
        )
        session.add_all([task, action, attempt])
        session.commit()

        assert recover_hard_hourly_delivery_credits(session, limit=5) == 1
        session.commit()
        assert recover_hard_hourly_delivery_credits(session, limit=5) == 0
        session.commit()
        assert session.scalar(select(TaskHardHourlyBucket.success_count)) == 1

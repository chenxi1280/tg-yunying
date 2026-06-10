from __future__ import annotations

from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Action, OperationTarget, Task, Tenant, TgAccount
from app.services._common import _now
from app.services.task_center import dispatcher
from app.services.task_center.channel_membership import channel_membership_summary, gate_channel_membership


def test_group_ai_membership_strategy_can_disable_auto_join_actions() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(OperationTarget(id=901, tenant_id=1, target_type="group", tg_peer_id="-100901", title="准入群", auth_status="已授权运营", can_send=True))
        session.add(TgAccount(id=11, tenant_id=1, display_name="账号11", phone_masked="11", status="在线", session_ciphertext="session"))
        task = Task(
            id="task-membership-off",
            tenant_id=1,
            name="关闭自动准入",
            type="group_ai_chat",
            status="running",
            account_config={"selection_mode": "all"},
            pacing_config={"mode": "fixed", "interval_seconds_min": 0, "interval_seconds_max": 0, "jitter_percent": 0},
            type_config={"target_operation_target_id": 901, "auto_join_target": False},
        )
        session.add(task)
        session.commit()

        result = gate_channel_membership(session, task, session.get(OperationTarget, 901), require_send=True)
        action_count = session.query(Action).filter(Action.task_id == task.id, Action.action_type == "ensure_target_membership").count()

    assert result.blocked is True
    assert result.created == 0
    assert action_count == 0
    assert task.last_error == "准入策略已关闭自动入群"


def test_group_ai_membership_strategy_disables_auto_follow_and_verification_helpers() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        task = Task(
            id="task-verification-off",
            tenant_id=1,
            name="关闭自动验证",
            type="group_ai_chat",
            status="running",
            type_config={"auto_follow_required_channel": False, "auto_resolve_verification": False},
        )
        session.add(task)
        action = Action(
            id="membership-verification-off",
            tenant_id=1,
            task_id=task.id,
            task_type="group_ai_chat",
            action_type="ensure_target_membership",
            account_id=11,
            status="pending",
        )
        session.add(action)
        session.commit()

        assert dispatcher._auto_follow_required_channel_enabled(session, action) is False
        assert dispatcher._auto_verification_enabled(session, action) is False


def test_membership_permission_denied_skip_counts_as_failed() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        target = OperationTarget(id=902, tenant_id=1, target_type="group", tg_peer_id="-100902", title="准入群", auth_status="已授权运营", can_send=True)
        account = TgAccount(id=12, tenant_id=1, display_name="账号12", phone_masked="12", status="在线", session_ciphertext="session")
        task = Task(id="task-permission-denied", tenant_id=1, name="权限失败", type="group_ai_chat", status="running", account_config={"selection_mode": "all"})
        session.add_all([target, account, task])
        session.add(
            Action(
                id="membership-permission-denied",
                tenant_id=1,
                task_id=task.id,
                task_type="group_ai_chat",
                action_type="ensure_target_membership",
                account_id=account.id,
                status="skipped",
                payload={"channel_target_id": target.id},
                result={"error_code": "membership_permission_denied", "membership_status": "permission_denied"},
            )
        )
        session.commit()

        summary = channel_membership_summary(session, 1, target, task.account_config, task_id=task.id, require_send=True)

    assert summary["failed_account_ids"] == [12]
    assert summary["failed_account_count"] == 1
    assert summary["need_join_account_count"] == 0


def test_hard_hourly_group_ai_fast_tracks_future_membership_actions() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        target = OperationTarget(id=903, tenant_id=1, target_type="group", tg_peer_id="-100903", title="硬目标群", auth_status="已授权运营", can_send=True)
        task = Task(
            id="task-hard-hourly-membership",
            tenant_id=1,
            name="硬目标 AI 群",
            type="group_ai_chat",
            status="running",
            account_config={"selection_mode": "all"},
            type_config={"target_operation_target_id": 903, "hard_hourly_target_enabled": True, "hourly_min_messages": 300},
        )
        session.add_all(
            [
                target,
                TgAccount(id=21, tenant_id=1, display_name="账号21", phone_masked="21", status="在线", session_ciphertext="session"),
                TgAccount(id=22, tenant_id=1, display_name="账号22", phone_masked="22", status="在线", session_ciphertext="session"),
                task,
                Action(
                    id="membership-future-21",
                    tenant_id=1,
                    task_id=task.id,
                    task_type="group_ai_chat",
                    action_type="ensure_target_membership",
                    account_id=21,
                    status="pending",
                    scheduled_at=now_value + timedelta(hours=8),
                    payload={"channel_target_id": target.id},
                ),
                Action(
                    id="membership-future-22",
                    tenant_id=1,
                    task_id=task.id,
                    task_type="group_ai_chat",
                    action_type="ensure_target_membership",
                    account_id=22,
                    status="pending",
                    scheduled_at=now_value + timedelta(hours=9),
                    payload={"channel_target_id": target.id},
                ),
            ]
        )
        session.commit()

        result = gate_channel_membership(session, task, target, require_send=True)
        rows = session.query(Action).filter(Action.task_id == task.id, Action.action_type == "ensure_target_membership").order_by(Action.scheduled_at.asc()).all()

    assert result.waiting is True
    assert [row.account_id for row in rows] == [21, 22]
    assert rows[0].scheduled_at <= now_value + timedelta(seconds=5)
    assert rows[1].scheduled_at <= now_value + timedelta(seconds=10)

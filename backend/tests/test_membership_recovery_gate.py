from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Action, OperationTarget, Task, Tenant, TgAccount, TgGroup, TgGroupAccount, VerificationTask
from app.services._common import _now
from app.services.task_center import membership_recovery_gate
from app.services.task_center.membership_recovery_gate import recover_missing_hard_hourly_memberships

pytestmark = pytest.mark.no_postgres


def test_recovery_creates_missing_hard_hourly_membership_actions() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        target = OperationTarget(
            id=901,
            tenant_id=1,
            target_type="group",
            tg_peer_id="-100901",
            title="青岛师范学院",
            auth_status="已授权运营",
            can_send=True,
        )
        task = Task(
            id="task-hard-hourly-recovery-membership",
            tenant_id=1,
            name="硬目标准入恢复",
            type="group_ai_chat",
            status="running",
            account_config={"selection_mode": "all"},
            type_config={
                "target_operation_target_id": 901,
                "hard_hourly_target_enabled": True,
                "hourly_min_messages": 60,
            },
            stats={"membership_need_join_count": 2},
        )
        session.add_all(
            [
                target,
                task,
                TgAccount(
                    id=51,
                    tenant_id=1,
                    display_name="账号51",
                    phone_masked="51",
                    status="在线",
                    session_ciphertext="session",
                ),
                TgAccount(
                    id=52,
                    tenant_id=1,
                    display_name="账号52",
                    phone_masked="52",
                    status="在线",
                    session_ciphertext="session",
                ),
            ]
        )
        session.commit()

        recovered = recover_missing_hard_hourly_memberships(session, limit=10)
        actions = (
            session.query(Action)
            .filter(Action.task_id == task.id, Action.action_type == "ensure_target_membership")
            .all()
        )

    assert recovered == 2
    assert {action.account_id for action in actions} == {51, 52}
    assert all(action.status == "pending" for action in actions)


def test_recovery_skips_future_hard_hourly_membership_checkpoint(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()
    monkeypatch.setattr(
        membership_recovery_gate,
        "gate_channel_membership",
        lambda *_args, **_kwargs: pytest.fail("future hard-hourly checkpoint must not enter membership recovery"),
    )

    with Session(engine) as session:
        session.add_all(
            [
                Tenant(id=1, name="默认运营空间"),
                OperationTarget(
                    id=911,
                    tenant_id=1,
                    target_type="group",
                    tg_peer_id="-100911",
                    title="未来复查群",
                    auth_status="已授权运营",
                    can_send=True,
                ),
                Task(
                    id="task-future-hard-hourly-membership-recovery",
                    tenant_id=1,
                    name="未来复查准入恢复",
                    type="group_ai_chat",
                    status="running",
                    hard_hourly_next_check_at=now_value + timedelta(minutes=5),
                    type_config={
                        "target_operation_target_id": 911,
                        "hard_hourly_target_enabled": True,
                        "hourly_min_messages": 60,
                    },
                    stats={"membership_need_join_count": 1},
                ),
            ]
        )
        session.commit()

        assert recover_missing_hard_hourly_memberships(session, limit=10) == 0


def test_recovery_rechecks_task_when_some_membership_actions_are_open() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        target = OperationTarget(
            id=902,
            tenant_id=1,
            target_type="group",
            tg_peer_id="-100902",
            title="青岛师范学院",
            auth_status="已授权运营",
            can_send=True,
        )
        task = Task(
            id="task-hard-hourly-partial-open-membership",
            tenant_id=1,
            name="硬目标准入部分 open 恢复",
            type="group_ai_chat",
            status="running",
            account_config={"selection_mode": "all"},
            type_config={
                "target_operation_target_id": 902,
                "hard_hourly_target_enabled": True,
                "hourly_min_messages": 60,
            },
            stats={"membership_need_join_count": 2},
        )
        payload = {
            "channel_id": "-100902",
            "channel_target_id": target.id,
            "target_type": "group",
            "target_display": target.title,
            "require_send": True,
        }
        session.add_all(
            [
                target,
                task,
                TgAccount(
                    id=61,
                    tenant_id=1,
                    display_name="账号61",
                    phone_masked="61",
                    status="在线",
                    session_ciphertext="session",
                ),
                TgAccount(
                    id=62,
                    tenant_id=1,
                    display_name="账号62",
                    phone_masked="62",
                    status="在线",
                    session_ciphertext="session",
                ),
                Action(
                    id="membership-open-61",
                    tenant_id=1,
                    task_id=task.id,
                    task_type=task.type,
                    action_type="ensure_target_membership",
                    account_id=61,
                    status="pending",
                    scheduled_at=now_value + timedelta(minutes=20),
                    payload=payload,
                ),
                Action(
                    id="membership-terminal-future-62",
                    tenant_id=1,
                    task_id=task.id,
                    task_type=task.type,
                    action_type="ensure_target_membership",
                    account_id=62,
                    status="skipped",
                    scheduled_at=now_value + timedelta(minutes=20),
                    created_at=now_value - timedelta(minutes=10),
                    payload=payload,
                    result={"membership_status": "not_joined"},
                ),
            ]
        )
        session.commit()

        recovered = recover_missing_hard_hourly_memberships(session, limit=10)
        actions = (
            session.query(Action)
            .filter(Action.task_id == task.id, Action.action_type == "ensure_target_membership")
            .order_by(Action.account_id.asc(), Action.created_at.asc())
            .all()
        )

    assert recovered == 1
    assert [(action.account_id, action.status) for action in actions] == [
        (61, "pending"),
        (62, "skipped"),
        (62, "pending"),
    ]


def test_recovery_retries_auto_verification_failures_when_no_membership_gap_remains() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    old_value = _now() - timedelta(minutes=10)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        target = OperationTarget(id=903, tenant_id=1, target_type="group", tg_peer_id="-100903", title="天津", auth_status="已授权运营", can_send=True)
        group = TgGroup(id=803, tenant_id=1, tg_peer_id="-100903", title="天津", auth_status="已授权运营", can_send=True)
        task = Task(
            id="task-hard-hourly-failed-membership-recovery",
            tenant_id=1,
            name="天津",
            type="group_ai_chat",
            status="running",
            account_config={"selection_mode": "all"},
            type_config={"target_operation_target_id": 903, "hard_hourly_target_enabled": True, "hourly_min_messages": 1},
            stats={"membership_need_join_count": 0, "membership_failed_count": 1},
        )
        ready = TgAccount(id=71, tenant_id=1, display_name="可发言", phone_masked="71", status="在线", session_ciphertext="session")
        failed = TgAccount(id=72, tenant_id=1, display_name="待修复", phone_masked="72", status="在线", session_ciphertext="session")
        payload = {"channel_id": "-100903", "channel_target_id": target.id, "target_type": "group", "target_display": target.title, "require_send": True}
        session.add_all(
            [
                target,
                group,
                task,
                ready,
                failed,
                TgGroupAccount(tenant_id=1, group_id=group.id, account_id=ready.id, can_send=True),
                Action(
                    id="membership-failed-72",
                    tenant_id=1,
                    task_id=task.id,
                    task_type=task.type,
                    action_type="ensure_target_membership",
                    account_id=failed.id,
                    status="skipped",
                    scheduled_at=old_value,
                    executed_at=old_value,
                    payload=payload,
                    result={"error_code": "membership_permission_denied", "membership_status": "permission_denied"},
                ),
                VerificationTask(
                    id=7300,
                    tenant_id=1,
                    account_id=failed.id,
                    group_id=group.id,
                    verification_type="群发言权限",
                    detected_reason="需要验证码",
                    suggested_action="识别图形验证码",
                    status="失败",
                    handled_at=old_value,
                ),
            ]
        )
        session.commit()

        recovered = recover_missing_hard_hourly_memberships(session, limit=10)
        actions = session.query(Action).filter(Action.task_id == task.id, Action.action_type == "ensure_target_membership").order_by(Action.created_at.asc()).all()
        retry_actions = [action for action in actions if action.account_id == failed.id and action.status == "pending"]

    assert recovered == 1
    assert len(retry_actions) == 1
    assert retry_actions[0].result["reactivated_reason"] == "hard_hourly_auto_verification_retry"

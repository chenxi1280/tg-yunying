from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.integrations.telegram import ChannelMembershipResult
from app.models import Action, ChannelMessage, OperationTarget, OperationTask, OperationTaskAttempt, Task, Tenant, TgAccount, TgGroup, TgGroupAccount
from app.schemas.operations import OperationTargetCreate
from app.services._common import _now
from app.services.operations import create_operation_target
from app.services.operations import _execute_operation_attempt
from app.services.task_center import dispatcher
from app.services.task_center.dispatcher import dispatch_action
from app.services.task_center.executors import build_task_plan
from app.services.task_center.service import get_task_detail


@pytest.fixture(autouse=True)
def clear_dispatcher_runtime_state():
    dispatcher._ACTION_RESERVATIONS.clear()
    dispatcher._IN_FLIGHT_ACCOUNTS.clear()
    yield
    dispatcher._ACTION_RESERVATIONS.clear()
    dispatcher._IN_FLIGHT_ACCOUNTS.clear()


def _engine():
    url = os.environ["TEST_DATABASE_URL"]
    engine = create_engine(url, future=True)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    return engine


def test_channel_target_create_recognizes_invite_link_and_public_username():
    engine = _engine()

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.commit()

        invite = create_operation_target(
            session,
            OperationTargetCreate(target_type="channel", tg_peer_id="https://t.me/+InviteHash123?single", title="邀请链接频道", can_send=False),
            "tester",
        )
        public = create_operation_target(
            session,
            OperationTargetCreate(target_type="channel", tg_peer_id="https://t.me/public_channel", title="公开频道", can_send=False),
            "tester",
        )

        assert invite.tg_peer_id == "https://t.me/+InviteHash123"
        assert invite.username == ""
        assert public.tg_peer_id == "public_channel"
        assert public.username == "public_channel"


def test_channel_task_runs_membership_precondition_before_main_actions(monkeypatch):
    engine = _engine()
    now_value = _now()

    monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        dispatcher.gateway,
        "ensure_channel_membership",
        lambda *args, **kwargs: ChannelMembershipResult(True, detail="joined", membership_status="joined"),
    )

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.flush()
        session.add_all(
            [
                TgAccount(id=11, tenant_id=1, display_name="已关注账号", phone_masked="+861***0011", status="在线"),
                TgAccount(id=12, tenant_id=1, display_name="待关注账号", phone_masked="+861***0012", status="在线"),
            ]
        )
        channel = OperationTarget(id=501, tenant_id=1, target_type="channel", tg_peer_id="pytest_member_channel", title="pytest 前置频道", username="pytest_member_channel", auth_status="未确认", can_send=False)
        group = TgGroup(id=601, tenant_id=1, tg_peer_id=channel.tg_peer_id, title=channel.title, group_type="channel", auth_status="已授权运营", can_send=True)
        task = Task(
            id="task-membership",
            tenant_id=1,
            name="membership",
            type="channel_view",
            status="running",
            next_run_at=now_value,
            account_config={"selection_mode": "manual", "account_ids": [11, 12], "max_concurrent": 2, "cooldown_per_account_minutes": 0},
            pacing_config={"mode": "fixed", "interval_seconds_min": 0, "interval_seconds_max": 0, "jitter_percent": 0},
            failure_policy={"max_retries": 0},
            type_config={"target_channel_id": 501, "message_scope": "specific", "message_ids": [701], "target_views_per_message": 2, "view_count_jitter": 0},
            stats={},
        )
        session.add_all([channel, group, task])
        session.flush()
        session.add_all([TgGroupAccount(tenant_id=1, group_id=601, account_id=11, can_send=True, permission_label="已关注"), ChannelMessage(id=701, tenant_id=1, channel_target_id=501, message_id=9001, content_preview="前置消息")])
        session.commit()

        assert build_task_plan(session, task) == 2
        assert session.query(Action).filter(Action.task_id == task.id, Action.action_type == "view_message").count() == 0
        membership_actions = list(session.query(Action).filter(Action.task_id == task.id, Action.action_type == "ensure_channel_membership").order_by(Action.account_id.asc()))
        assert [action.status for action in membership_actions] == ["skipped", "pending"]
        assert membership_actions[0].result["membership_status"] == "already_joined"

        dispatch_action(session, membership_actions[1])
        assert build_task_plan(session, task) == 2
        view_actions = list(session.query(Action).filter(Action.task_id == task.id, Action.action_type == "view_message").order_by(Action.account_id.asc()))
        assert [action.account_id for action in view_actions] == [11, 12]
        assert task.stats["membership_stage"] == "membership_ready"
        session.refresh(channel)
        assert channel.can_send is False
        detail = get_task_detail(session, 1, task.id)
        assert detail["membership_phase"]["stage"] == "membership_ready"
        assert {item["membership_status"] for item in detail["membership_accounts"]} >= {"already_joined", "joined"}


def test_channel_task_blocks_main_actions_when_all_membership_fails(monkeypatch):
    engine = _engine()
    now_value = _now()

    monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        dispatcher.gateway,
        "ensure_channel_membership",
        lambda *args, **kwargs: ChannelMembershipResult(False, "失败", "目标无效", "邀请链接失效", "failed"),
    )

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.flush()
        session.add(TgAccount(id=21, tenant_id=1, display_name="失败账号", phone_masked="+861***0021", status="在线"))
        session.add(OperationTarget(id=502, tenant_id=1, target_type="channel", tg_peer_id="blocked-channel", title="失效频道", username="", auth_status="未确认", can_send=False))
        task = Task(
            id="task-membership-blocked",
            tenant_id=1,
            name="membership blocked",
            type="channel_like",
            status="running",
            next_run_at=now_value,
            account_config={"selection_mode": "manual", "account_ids": [21], "max_concurrent": 1, "cooldown_per_account_minutes": 0},
            pacing_config={"mode": "fixed", "interval_seconds_min": 0, "interval_seconds_max": 0, "jitter_percent": 0},
            failure_policy={"max_retries": 0},
            type_config={"target_channel_id": 502, "message_scope": "specific", "message_ids": [702], "target_likes_per_message": 1, "like_count_jitter": 0, "allowed_reactions": ["👍"]},
            stats={},
        )
        session.add(task)
        session.flush()
        session.add(ChannelMessage(id=702, tenant_id=1, channel_target_id=502, message_id=9002, content_preview="失败消息"))
        session.commit()

        assert build_task_plan(session, task) == 1
        action = session.query(Action).filter(Action.task_id == task.id, Action.action_type == "ensure_channel_membership").one()
        dispatch_action(session, action)

        assert build_task_plan(session, task) == 0
        assert session.query(Action).filter(Action.task_id == task.id, Action.action_type == "like_message").count() == 0
        assert task.stats["membership_stage"] == "membership_blocked"
        assert task.last_error == "没有账号成功关注目标频道"


def test_authorized_sendable_channel_still_requires_per_account_membership():
    engine = _engine()
    now_value = _now()

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.flush()
        session.add(TgAccount(id=25, tenant_id=1, display_name="未关注账号", phone_masked="+861***0025", status="在线"))
        session.add(OperationTarget(id=525, tenant_id=1, target_type="channel", tg_peer_id="authorized-channel", title="已授权频道", username="authorized_channel", auth_status="已授权运营", can_send=True))
        task = Task(
            id="task-authorized-channel-membership",
            tenant_id=1,
            name="authorized membership",
            type="channel_view",
            status="running",
            next_run_at=now_value,
            account_config={"selection_mode": "manual", "account_ids": [25], "max_concurrent": 1, "cooldown_per_account_minutes": 0},
            pacing_config={"mode": "fixed", "interval_seconds_min": 0, "interval_seconds_max": 0, "jitter_percent": 0},
            failure_policy={"max_retries": 0},
            type_config={"target_channel_id": 525, "message_scope": "specific", "message_ids": [725], "target_views_per_message": 1, "view_count_jitter": 0},
            stats={},
        )
        session.add(task)
        session.flush()
        session.add(ChannelMessage(id=725, tenant_id=1, channel_target_id=525, message_id=9025, content_preview="已授权频道消息"))
        session.commit()

        assert build_task_plan(session, task) == 1
        assert session.query(Action).filter(Action.task_id == task.id, Action.action_type == "ensure_channel_membership").count() == 1
        assert session.query(Action).filter(Action.task_id == task.id, Action.action_type == "view_message").count() == 0


def test_channel_main_action_runtime_guard_blocks_unjoined_account(monkeypatch):
    engine = _engine()
    now_value = _now()

    monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        dispatcher.gateway,
        "view_channel_message",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unjoined account must not reach gateway")),
    )

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.flush()
        session.add(TgAccount(id=31, tenant_id=1, display_name="未关注账号", phone_masked="+861***0031", status="在线"))
        session.add(OperationTarget(id=503, tenant_id=1, target_type="channel", tg_peer_id="guard-channel", title="运行时守卫频道", username="guard_channel", auth_status="已授权运营", can_send=False))
        task = Task(id="task-runtime-guard", tenant_id=1, name="runtime guard", type="channel_view", status="running", account_config={}, pacing_config={}, failure_policy={})
        session.add(task)
        session.flush()
        action = Action(
            id="action-runtime-guard",
            tenant_id=1,
            task_id=task.id,
            task_type=task.type,
            action_type="view_message",
            account_id=31,
            status="pending",
            scheduled_at=now_value,
            payload={"channel_id": "guard-channel", "channel_target_id": 503, "message_id": 1},
        )
        session.add(action)
        session.commit()

        dispatch_action(session, action)
        assert action.status == "failed"
        assert action.result["validation_stage"] == "account_channel_membership"


def test_legacy_channel_attempt_runtime_guard_blocks_unjoined_account():
    engine = _engine()

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.flush()
        session.add(TgAccount(id=41, tenant_id=1, display_name="旧任务未关注账号", phone_masked="+861***0041", status="在线"))
        channel = OperationTarget(id=504, tenant_id=1, target_type="channel", tg_peer_id="legacy-guard-channel", title="旧任务频道", username="legacy_guard", auth_status="已授权运营", can_send=False)
        session.add(channel)
        session.flush()
        message = ChannelMessage(id=704, tenant_id=1, channel_target_id=504, message_id=9104, content_preview="旧任务消息")
        session.add(message)
        session.flush()
        task = OperationTask(id=804, tenant_id=1, task_type="CHANNEL_VIEW", channel_message_id=704, title="legacy")
        session.add(task)
        session.flush()
        attempt = OperationTaskAttempt(
            id=904,
            tenant_id=1,
            task_id=804,
            account_id=41,
            action_type="view",
            content="",
            reaction="",
            status="排队中",
        )
        session.add(attempt)
        session.commit()

        ok, failure_type, detail = _execute_operation_attempt(session, task, attempt, None, message, channel)
        assert ok is False
        assert failure_type == "账号不可用"
        assert "未关注目标频道" in detail

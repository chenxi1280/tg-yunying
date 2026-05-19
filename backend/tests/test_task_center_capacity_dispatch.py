from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.integrations.telegram import ChannelMembershipResult, OperationResult, SendResult
from app.models import Action, ChannelMessage, DailyRuntimeStat, ExecutionAttempt, GroupContextMessage, OperationTarget, OperationTask, OperationTaskAttempt, ReviewQueue, RuntimeCleanupAudit, SchedulingSetting, Task, Tenant, TgAccount, TgGroup, TgGroupAccount
from app.services._common import _now
from app.services.task_center import dispatcher
from app.services.task_center import account_pool
from app.services.task_center.dispatcher import claim_actions, dispatch_action
from app.services.task_center.executors import build_task_plan
from app.services.task_center.runtime_retention import cleanup_runtime_details
from app.services.task_center.service import _recover_stale_executing_actions, get_task_detail
from app.services.operations import _execute_operation_attempt


@pytest.fixture(autouse=True)
def clear_dispatcher_runtime_state():
    dispatcher._ACTION_RESERVATIONS.clear()
    dispatcher._IN_FLIGHT_ACCOUNTS.clear()
    yield
    dispatcher._ACTION_RESERVATIONS.clear()
    dispatcher._IN_FLIGHT_ACCOUNTS.clear()


class FakeRedisTokenBucket:
    def __init__(self, *, blocked_key: str = "", wait_seconds: int = 7) -> None:
        self.blocked_key = blocked_key
        self.wait_seconds = wait_seconds
        self.bucket_keys: list[str] = []
        self.reservation_keys: list[str] = []
        self.released_keys: list[str] = []

    def eval(self, _script, numkeys, *args):  # noqa: ANN001
        if numkeys == 1:
            self.released_keys.append(str(args[0]))
            return 1
        bucket_key = str(args[0])
        reservation_key = str(args[1])
        self.bucket_keys.append(bucket_key)
        if bucket_key == self.blocked_key:
            return [0, self.wait_seconds]
        self.reservation_keys.append(reservation_key)
        return [1, 0]


class FakeRedisAccountLock:
    def __init__(self, *, locked: bool = True) -> None:
        self.locked = locked
        self.set_calls: list[tuple[str, str, bool, int]] = []
        self.released_keys: list[str] = []

    def set(self, key, token, *, nx, ex):  # noqa: ANN001
        self.set_calls.append((str(key), str(token), bool(nx), int(ex)))
        return self.locked

    def eval(self, _script, numkeys, *args):  # noqa: ANN001
        if numkeys == 1:
            self.released_keys.append(str(args[0]))
        return 1


def _redis_bucket_settings(**overrides):
    defaults = {
        "enable_redis_token_bucket": True,
        "redis_url": "redis://test",
        "redis_token_fail_closed": True,
        "action_claim_seconds": 60,
        "global_tg_rate_per_second": 10,
        "task_rate_per_minute": 60,
        "task_type_rate_per_minute": 120,
        "account_rate_per_hour": 120,
        "proxy_rate_per_minute": 0,
        "target_rate_per_minute": 30,
        "media_rate_per_minute": 20,
        "task_type_token_weights": "group_relay=2,group_ai_chat=2",
        "action_claim_limit": 100,
        "action_lease_seconds": 1800,
        "enable_redis_account_inflight": False,
        "redis_account_inflight_seconds": 1800,
        "account_shard_total": 1,
        "account_shard_index": 0,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_claim_actions_uses_claiming_then_confirms_executing_with_account_lock():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgAccount(id=11, tenant_id=1, display_name="账号", phone_masked="+861***0011", status="在线"))
        session.add(Task(id="task-claim", tenant_id=1, name="claim", type="group_ai_chat", status="running", priority=1))
        session.add_all(
            [
                Action(id="action-1", tenant_id=1, task_id="task-claim", task_type="group_ai_chat", action_type="send_message", account_id=11, status="pending", scheduled_at=now_value, payload={"message_text": "1"}),
                Action(id="action-2", tenant_id=1, task_id="task-claim", task_type="group_ai_chat", action_type="send_message", account_id=11, status="pending", scheduled_at=now_value, payload={"message_text": "2"}),
            ]
        )
        session.commit()

        claimed = claim_actions(session, limit=2, worker_id="worker-test")

        assert [action.id for action in claimed] == ["action-1"]
        assert session.get(Action, "action-1").status == "executing"
        deferred = session.get(Action, "action-2")
        assert deferred.status == "pending"
        assert deferred.scheduled_at <= _now()
        assert deferred.result["claim_released_reason"] == "account_inflight_conflict"
        dispatcher._ACTION_RESERVATIONS.clear()
        dispatcher._IN_FLIGHT_ACCOUNTS.clear()


def test_channel_task_runs_membership_precondition_before_main_actions(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()

    monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        dispatcher.gateway,
        "ensure_channel_membership",
        lambda *args, **kwargs: ChannelMembershipResult(True, detail="joined", membership_status="joined"),
    )

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add_all(
            [
                TgAccount(id=11, tenant_id=1, display_name="已关注账号", phone_masked="+861***0011", status="在线"),
                TgAccount(id=12, tenant_id=1, display_name="待关注账号", phone_masked="+861***0012", status="在线"),
            ]
        )
        channel = OperationTarget(id=501, tenant_id=1, target_type="channel", tg_peer_id="pytest_member_channel", title="pytest 前置频道", username="pytest_member_channel", auth_status="未确认", can_send=False)
        group = TgGroup(id=601, tenant_id=1, tg_peer_id=channel.tg_peer_id, title=channel.title, group_type="channel", auth_status="已授权运营", can_send=True)
        session.add_all([channel, group, TgGroupAccount(tenant_id=1, group_id=601, account_id=11, can_send=True, permission_label="已关注")])
        session.add(ChannelMessage(id=701, tenant_id=1, channel_target_id=501, message_id=9001, content_preview="前置消息"))
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
        session.add(task)
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
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()

    monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        dispatcher.gateway,
        "ensure_channel_membership",
        lambda *args, **kwargs: ChannelMembershipResult(False, "失败", "目标无效", "邀请链接失效", "failed"),
    )

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgAccount(id=21, tenant_id=1, display_name="失败账号", phone_masked="+861***0021", status="在线"))
        session.add(OperationTarget(id=502, tenant_id=1, target_type="channel", tg_peer_id="blocked-channel", title="失效频道", username="", auth_status="未确认", can_send=False))
        session.add(ChannelMessage(id=702, tenant_id=1, channel_target_id=502, message_id=9002, content_preview="失败消息"))
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
        session.commit()

        assert build_task_plan(session, task) == 1
        action = session.query(Action).filter(Action.task_id == task.id, Action.action_type == "ensure_channel_membership").one()
        dispatch_action(session, action)

        assert build_task_plan(session, task) == 0
        assert session.query(Action).filter(Action.task_id == task.id, Action.action_type == "like_message").count() == 0
        assert task.stats["membership_stage"] == "membership_blocked"
        assert task.last_error == "没有账号成功关注目标频道"


def test_authorized_sendable_channel_still_requires_per_account_membership():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgAccount(id=25, tenant_id=1, display_name="未关注账号", phone_masked="+861***0025", status="在线"))
        session.add(OperationTarget(id=525, tenant_id=1, target_type="channel", tg_peer_id="authorized-channel", title="已授权频道", username="authorized_channel", auth_status="已授权运营", can_send=True))
        session.add(ChannelMessage(id=725, tenant_id=1, channel_target_id=525, message_id=9025, content_preview="已授权频道消息"))
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
        session.commit()

        assert build_task_plan(session, task) == 1
        assert session.query(Action).filter(Action.task_id == task.id, Action.action_type == "ensure_channel_membership").count() == 1
        assert session.query(Action).filter(Action.task_id == task.id, Action.action_type == "view_message").count() == 0


def test_channel_main_action_runtime_guard_blocks_unjoined_account(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()

    monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        dispatcher.gateway,
        "view_channel_message",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unjoined account must not reach gateway")),
    )

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgAccount(id=31, tenant_id=1, display_name="未关注账号", phone_masked="+861***0031", status="在线"))
        session.add(OperationTarget(id=503, tenant_id=1, target_type="channel", tg_peer_id="guard-channel", title="运行时守卫频道", username="guard_channel", auth_status="已授权运营", can_send=False))
        task = Task(id="task-runtime-guard", tenant_id=1, name="runtime guard", type="channel_view", status="running", account_config={}, pacing_config={}, failure_policy={})
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
        session.add_all([task, action])
        session.commit()

        dispatch_action(session, action)
        assert action.status == "failed"
        assert action.result["validation_stage"] == "account_channel_membership"


def test_legacy_channel_attempt_runtime_guard_blocks_unjoined_account():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgAccount(id=41, tenant_id=1, display_name="旧任务未关注账号", phone_masked="+861***0041", status="在线"))
        channel = OperationTarget(id=504, tenant_id=1, target_type="channel", tg_peer_id="legacy-guard-channel", title="旧任务频道", username="legacy_guard", auth_status="已授权运营", can_send=False)
        message = ChannelMessage(id=704, tenant_id=1, channel_target_id=504, message_id=9104, content_preview="旧任务消息")
        task = OperationTask(id=804, tenant_id=1, task_type="CHANNEL_VIEW", channel_message_id=704, title="legacy")
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
        session.add_all([channel, message, task, attempt])
        session.commit()

        ok, failure_type, detail = _execute_operation_attempt(session, task, attempt, None, message, channel)
        assert ok is False
        assert failure_type == "账号不可用"
        assert "未关注目标频道" in detail


def test_claim_actions_reassigns_account_before_reserving_runtime_resources_and_dispatches(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()
    sent: dict[str, int] = {}

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(SchedulingSetting(tenant_id=1, default_account_cooldown_seconds=3600, jitter_min_seconds=0, jitter_max_seconds=0))
        session.add_all(
            [
                TgAccount(id=11, tenant_id=1, display_name="账号A", phone_masked="+861***0011", status="在线", session_ciphertext="session-a"),
                TgAccount(id=12, tenant_id=1, display_name="账号B", phone_masked="+861***0012", status="在线", session_ciphertext="session-b"),
            ]
        )
        session.add(Task(id="task-reassign", tenant_id=1, name="claim", type="group_ai_chat", status="running", priority=1, account_config={"selection_mode": "manual", "account_ids": [11, 12], "max_concurrent": 2}))
        session.add(Action(id="old-success", tenant_id=1, task_id="task-reassign", task_type="group_ai_chat", action_type="send_message", account_id=11, status="success", scheduled_at=now_value - timedelta(minutes=1), executed_at=now_value - timedelta(minutes=1), payload={"chat_id": "-1001", "message_text": "old"}))
        session.add(Action(id="action-reassign", tenant_id=1, task_id="task-reassign", task_type="group_ai_chat", action_type="send_message", account_id=11, status="pending", scheduled_at=now_value, payload={"chat_id": "-1001", "message_text": "new"}))
        session.commit()

        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *args, **kwargs: object())

        def fake_send_to_target(account_id, *_args, **_kwargs):  # noqa: ANN001
            sent["account_id"] = account_id
            return SendResult(True, remote_message_id="tg-reassigned")

        monkeypatch.setattr(dispatcher.gateway, "send_message_to_target", fake_send_to_target)

        claimed = claim_actions(session, limit=1, worker_id="worker-test")

        assert [action.id for action in claimed] == ["action-reassign"]
        action = session.get(Action, "action-reassign")
        assert action.account_id == 12
        assert action.status == "executing"
        assert 11 not in dispatcher._IN_FLIGHT_ACCOUNTS
        assert 12 in dispatcher._IN_FLIGHT_ACCOUNTS

        assert dispatcher.dispatch_action(session, action) is True

        assert sent["account_id"] == 12
        assert action.status == "success"
        assert action.result["original_account_id"] == 11
        assert action.result["reassigned_account_id"] == 12
        assert action.result["telegram_msg_id"] == "tg-reassigned"
        assert 12 not in dispatcher._IN_FLIGHT_ACCOUNTS
        assert action.id not in dispatcher._ACTION_RESERVATIONS


def test_dispatch_context_expired_skip_releases_reserved_account_runtime_resource(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(Task(id="task-skip", tenant_id=1, name="skip", type="group_ai_chat", status="running"))
        session.add(TgAccount(id=11, tenant_id=1, display_name="账号", phone_masked="+861***0011", status="在线", session_ciphertext="session"))
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="运营群", auth_status="已授权运营", can_send=True, require_review=False))
        session.add(TgGroupAccount(tenant_id=1, group_id=7, account_id=11, can_send=True))
        old_context = GroupContextMessage(tenant_id=1, group_id=7, listener_account_id=11, content="旧上下文", remote_message_id="old", created_at=now_value - timedelta(minutes=2))
        new_context = GroupContextMessage(tenant_id=1, group_id=7, listener_account_id=11, content="新上下文", remote_message_id="new", created_at=now_value)
        session.add_all([old_context, new_context])
        session.flush()
        action = Action(
            id="action-skip",
            tenant_id=1,
            task_id="task-skip",
            task_type="group_ai_chat",
            action_type="send_message",
            account_id=11,
            status="pending",
            scheduled_at=now_value,
            payload={
                "group_id": 7,
                "message_text": "skip",
                "review_approved": True,
                "cycle_id": "cycle-skip",
                "context_snapshot_message_id": old_context.id,
                "context_expire_after_messages": 1,
            },
        )
        session.add(action)
        session.commit()

        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *args, **kwargs: object())
        monkeypatch.setattr(dispatcher.gateway, "send_message", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("context expired action must not call TG")))

        [claimed] = claim_actions(session, limit=1, worker_id="worker-test")
        assert 11 in dispatcher._IN_FLIGHT_ACCOUNTS
        assert claimed.id in dispatcher._ACTION_RESERVATIONS

        assert dispatcher.dispatch_action(session, claimed) is True

        assert claimed.status == "skipped"
        assert claimed.result["error_code"] == "context_expired"
        assert 11 not in dispatcher._IN_FLIGHT_ACCOUNTS
        assert claimed.id not in dispatcher._ACTION_RESERVATIONS


def test_gateway_exception_after_call_started_marks_unknown_after_send(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(Task(id="task-unknown", tenant_id=1, name="unknown", type="group_relay", status="running"))
        session.add(TgAccount(id=11, tenant_id=1, display_name="账号", phone_masked="+861***0011", status="在线", session_ciphertext="session"))
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="运营群", auth_status="已授权运营", can_send=True, require_review=False))
        session.add(TgGroupAccount(tenant_id=1, group_id=7, account_id=11, can_send=True))
        session.add(
            Action(
                id="action-unknown",
                tenant_id=1,
                task_id="task-unknown",
                task_type="group_relay",
                action_type="send_message",
                account_id=11,
                status="pending",
                scheduled_at=now_value,
                payload={"group_id": 7, "message_text": "hello", "review_approved": True},
            )
        )
        session.commit()

        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *args, **kwargs: object())
        monkeypatch.setattr(dispatcher.gateway, "send_message", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("socket lost after send")))

        [claimed] = claim_actions(session, limit=1, worker_id="worker-test")
        assert dispatcher.dispatch_action(session, claimed) is True

        refreshed = session.get(Action, "action-unknown")
        assert refreshed.status == "unknown_after_send"
        assert refreshed.result["error_code"] == "unknown_after_send"
        assert 11 not in dispatcher._IN_FLIGHT_ACCOUNTS
        assert refreshed.id not in dispatcher._ACTION_RESERVATIONS


def test_claim_actions_db_unique_index_blocks_cross_worker_same_account_execution():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgAccount(id=11, tenant_id=1, display_name="账号", phone_masked="+861***0011", status="在线"))
        session.add(Task(id="task-workers", tenant_id=1, name="multi-worker", type="group_relay", status="running", priority=1))
        session.add_all(
            [
                Action(id="action-worker-a", tenant_id=1, task_id="task-workers", task_type="group_relay", action_type="send_message", account_id=11, status="executing", scheduled_at=now_value, payload={"chat_id": "-1001", "message_text": "a"}),
                Action(id="action-worker-b", tenant_id=1, task_id="task-workers", task_type="group_relay", action_type="send_message", account_id=11, status="pending", scheduled_at=now_value, payload={"chat_id": "-1001", "message_text": "b"}),
            ]
        )
        session.commit()

        claimed = claim_actions(session, limit=1, worker_id="worker-b")

        assert claimed == []
        blocked = session.get(Action, "action-worker-b")
        assert blocked.status == "pending"
        assert blocked.result["claim_released_reason"] == "account_inflight_conflict"
        assert 11 not in dispatcher._IN_FLIGHT_ACCOUNTS
        assert blocked.id not in dispatcher._ACTION_RESERVATIONS


def test_claimed_action_dispatch_success_releases_runtime_reservation(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgAccount(id=11, tenant_id=1, display_name="账号", phone_masked="+861***0011", status="在线", session_ciphertext="session"))
        session.add(Task(id="task-dispatch", tenant_id=1, name="dispatch", type="group_relay", status="running", priority=1))
        session.add(Action(id="action-dispatch", tenant_id=1, task_id="task-dispatch", task_type="group_relay", action_type="send_message", account_id=11, status="pending", scheduled_at=now_value, payload={"chat_id": "-1001", "message_text": "hello"}))
        session.commit()

        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *args, **kwargs: object())
        monkeypatch.setattr(dispatcher.gateway, "send_message_to_target", lambda *args, **kwargs: SendResult(True, remote_message_id="tg-ok"))

        [action] = claim_actions(session, limit=1, worker_id="worker-test")
        assert 11 in dispatcher._IN_FLIGHT_ACCOUNTS
        assert action.id in dispatcher._ACTION_RESERVATIONS

        assert dispatcher.dispatch_action(session, action) is True

        assert action.status == "success"
        assert action.result["telegram_msg_id"] == "tg-ok"
        assert 11 not in dispatcher._IN_FLIGHT_ACCOUNTS
        assert action.id not in dispatcher._ACTION_RESERVATIONS


def test_claim_actions_gets_multi_dimension_redis_token_bucket_before_executing(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()
    fake_redis = FakeRedisTokenBucket()
    monkeypatch.setattr(dispatcher, "get_settings", lambda: _redis_bucket_settings())
    monkeypatch.setattr(dispatcher, "_redis_client", lambda _redis_url: fake_redis)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgAccount(id=11, tenant_id=1, display_name="账号", phone_masked="+861***0011", status="在线"))
        session.add(Task(id="task-redis", tenant_id=1, name="redis", type="group_relay", status="running", priority=1))
        session.add(Action(id="action-redis", tenant_id=1, task_id="task-redis", task_type="group_relay", action_type="send_message", account_id=11, status="pending", scheduled_at=now_value, payload={"chat_id": "-1001", "message_text": "hello", "media_segments": [{"source": "tg-cache://cache/1"}]}))
        session.commit()

        [action] = claim_actions(session, limit=1, worker_id="worker-test")

        assert action.status == "executing"
        assert set(fake_redis.bucket_keys) == {
            "rate:global:tg_api",
            "rate:task:task-redis",
            "rate:task_type:group_relay",
            "rate:account:11",
            "rate:target:-1001",
            "rate:media",
        }
        assert len(dispatcher._ACTION_RESERVATIONS[action.id].redis_reservations) == 6
        dispatcher._release_runtime_resources(action)
    assert fake_redis.released_keys == fake_redis.reservation_keys


def test_claim_actions_filters_accounts_by_current_shard(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()
    settings = _redis_bucket_settings(enable_redis_token_bucket=False, account_shard_total=2, account_shard_index=1)
    monkeypatch.setattr(dispatcher, "get_settings", lambda: settings)
    monkeypatch.setattr(account_pool, "get_settings", lambda: settings)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add_all(
            [
                TgAccount(id=10, tenant_id=1, display_name="账号A", phone_masked="+861***0010", status="在线"),
                TgAccount(id=11, tenant_id=1, display_name="账号B", phone_masked="+861***0011", status="在线"),
            ]
        )
        session.add(Task(id="task-shard", tenant_id=1, name="shard", type="group_relay", status="running", priority=1))
        session.add_all(
            [
                Action(id="action-shard-0", tenant_id=1, task_id="task-shard", task_type="group_relay", action_type="send_message", account_id=10, status="pending", scheduled_at=now_value, payload={"chat_id": "-1001", "message_text": "a"}),
                Action(id="action-shard-1", tenant_id=1, task_id="task-shard", task_type="group_relay", action_type="send_message", account_id=11, status="pending", scheduled_at=now_value, payload={"chat_id": "-1001", "message_text": "b"}),
            ]
        )
        session.commit()

        claimed = claim_actions(session, limit=5, worker_id="worker-shard")

        assert [action.id for action in claimed] == ["action-shard-1"]
        assert session.get(Action, "action-shard-0").status == "pending"
        dispatcher._ACTION_RESERVATIONS.clear()
        dispatcher._IN_FLIGHT_ACCOUNTS.clear()


def test_claim_actions_uses_redis_account_inflight_lock(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()
    fake_redis = FakeRedisAccountLock(locked=False)
    settings = _redis_bucket_settings(enable_redis_account_inflight=True)
    monkeypatch.setattr(dispatcher, "get_settings", lambda: settings)
    monkeypatch.setattr(dispatcher, "_redis_client", lambda _redis_url: fake_redis)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgAccount(id=11, tenant_id=1, display_name="账号", phone_masked="+861***0011", status="在线"))
        session.add(Task(id="task-redis-account-lock", tenant_id=1, name="redis account lock", type="group_relay", status="running", priority=1))
        session.add(Action(id="action-redis-account-lock", tenant_id=1, task_id="task-redis-account-lock", task_type="group_relay", action_type="send_message", account_id=11, status="pending", scheduled_at=now_value, payload={"chat_id": "-1001", "message_text": "hello"}))
        session.commit()

        claimed = claim_actions(session, limit=1, worker_id="worker-test")

        action = session.get(Action, "action-redis-account-lock")
        assert claimed == []
        assert action.status == "pending"
        assert action.result["claim_released_reason"] == "account_inflight_conflict"
        assert fake_redis.set_calls[0][0] == "inflight:account:11"
        assert 11 not in dispatcher._IN_FLIGHT_ACCOUNTS


def test_claim_actions_keeps_pending_and_delays_when_redis_token_bucket_is_limited(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()
    fake_redis = FakeRedisTokenBucket(blocked_key="rate:account:11", wait_seconds=9)
    monkeypatch.setattr(dispatcher, "get_settings", lambda: _redis_bucket_settings())
    monkeypatch.setattr(dispatcher, "_redis_client", lambda _redis_url: fake_redis)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgAccount(id=11, tenant_id=1, display_name="账号", phone_masked="+861***0011", status="在线"))
        session.add(Task(id="task-redis-wait", tenant_id=1, name="redis wait", type="group_relay", status="running", priority=1))
        session.add(Action(id="action-redis-wait", tenant_id=1, task_id="task-redis-wait", task_type="group_relay", action_type="send_message", account_id=11, status="pending", scheduled_at=now_value, payload={"chat_id": "-1001", "message_text": "hello"}))
        session.commit()

        claimed = claim_actions(session, limit=1, worker_id="worker-test")

        action = session.get(Action, "action-redis-wait")
        assert claimed == []
        assert action.status == "pending"
        assert action.scheduled_at >= now_value + timedelta(seconds=9)
        assert action.result["claim_released_reason"] == "redis_token_bucket_limited"
        assert action.result["rate_limit_key"] == "rate:account:11"
        assert 11 not in dispatcher._IN_FLIGHT_ACCOUNTS
        assert action.id not in dispatcher._ACTION_RESERVATIONS
        assert fake_redis.released_keys == fake_redis.reservation_keys


def test_recovery_marks_gateway_started_attempt_unknown_after_send():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(Task(id="task-unknown", tenant_id=1, name="unknown", type="group_relay", status="running", stats={}))
        session.add(
            Action(
                id="action-unknown",
                tenant_id=1,
                task_id="task-unknown",
                task_type="group_relay",
                action_type="send_message",
                status="executing",
                scheduled_at=now_value - timedelta(hours=1),
                lease_owner="worker-a",
                lease_expires_at=now_value - timedelta(minutes=1),
                payload={"chat_id": "-1001", "message_text": "hello"},
                result={},
            )
        )
        session.add(
            ExecutionAttempt(
                id="attempt-unknown",
                tenant_id=1,
                action_id="action-unknown",
                worker_id="worker-a",
                attempt_no=1,
                status="gateway_call_started",
                before_call_at=now_value - timedelta(minutes=5),
                gateway_call_started_at=now_value - timedelta(minutes=5),
            )
        )
        session.commit()

        assert _recover_stale_executing_actions(session, timeout_minutes=30) == 1

        action = session.get(Action, "action-unknown")
        attempt = session.get(ExecutionAttempt, "attempt-unknown")
        assert action.status == "unknown_after_send"
        assert action.result["error_code"] == "unknown_after_send"
        assert attempt.status == "result_unknown"


def test_runtime_cleanup_summarizes_then_deletes_all_window_out_details():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    today = datetime(2026, 5, 15).date()
    old_at = datetime(2026, 5, 9, 10, 0, 0)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(Task(id="task-clean", tenant_id=1, name="clean", type="group_relay", status="running"))
        session.add_all(
            [
                Action(id="old-success", tenant_id=1, task_id="task-clean", task_type="group_relay", action_type="send_message", account_id=11, status="success", scheduled_at=old_at, executed_at=old_at, payload={"group_id": 7}),
                Action(id="old-unknown", tenant_id=1, task_id="task-clean", task_type="group_relay", action_type="send_message", account_id=11, status="unknown_after_send", scheduled_at=old_at, payload={"group_id": 7}),
            ]
        )
        session.add(ExecutionAttempt(id="old-attempt", tenant_id=1, action_id="old-unknown", attempt_no=1, status="result_unknown"))
        session.add(ReviewQueue(id="old-review", tenant_id=1, task_id="task-clean", action_id="old-unknown", status="pending"))
        session.commit()

        deleted = cleanup_runtime_details(session, retention_days=5, today=today)
        session.commit()

        assert deleted == 4
        assert session.get(Action, "old-success") is None
        assert session.get(Action, "old-unknown") is None
        assert session.get(ExecutionAttempt, "old-attempt") is None
        assert session.get(ReviewQueue, "old-review") is None
        assert session.query(RuntimeCleanupAudit).count() == 1
        global_unknown = session.query(DailyRuntimeStat).filter_by(stat_date=old_at.date(), dimension_type="global", dimension_id="all", metric_name="status.unknown_after_send").one()
        assert global_unknown.metric_value == 1

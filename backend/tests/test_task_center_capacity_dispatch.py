from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.integrations.telegram import OperationResult, SendResult
from app.models import Action, DailyRuntimeStat, ExecutionAttempt, GroupContextMessage, OperationTarget, ReviewQueue, RuntimeCleanupAudit, SchedulingSetting, Task, Tenant, TgAccount, TgGroup, TgGroupAccount, VerificationTask
from app.services._common import _now
from app.services.task_center import dispatcher
from app.services.task_center import account_pool
from app.services.task_center.dispatcher import claim_actions
from app.services.task_center.runtime_retention import cleanup_runtime_details
from app.services.task_center.service import _recover_stale_executing_actions
from app.services.task_center.stats import refresh_task_stats


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


def test_claim_actions_reassigns_group_send_action_when_account_lost_permission(monkeypatch):
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
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="运营群", auth_status="已授权运营", can_send=True, require_review=False))
        session.add_all(
            [
                TgGroupAccount(tenant_id=1, group_id=7, account_id=11, can_send=False, permission_label="群无权限或账号不可发言"),
                TgGroupAccount(tenant_id=1, group_id=7, account_id=12, can_send=True, permission_label="可发言"),
            ]
        )
        session.add(Task(id="task-group-reassign", tenant_id=1, name="claim", type="group_ai_chat", status="running", priority=1, account_config={"selection_mode": "all", "max_concurrent": 2}))
        session.add(Action(id="action-group-reassign", tenant_id=1, task_id="task-group-reassign", task_type="group_ai_chat", action_type="send_message", account_id=11, status="pending", scheduled_at=now_value, payload={"group_id": 7, "message_text": "new", "review_approved": True}))
        session.commit()

        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *args, **kwargs: object())

        def fake_send_message(account_id, *_args, **_kwargs):  # noqa: ANN001
            sent["account_id"] = account_id
            return SendResult(True, remote_message_id="tg-group-reassigned")

        monkeypatch.setattr(dispatcher.gateway, "send_message", fake_send_message)

        [claimed] = claim_actions(session, limit=1, worker_id="worker-test")

        assert claimed.account_id == 12
        assert dispatcher.dispatch_action(session, claimed) is True

        action = session.get(Action, "action-group-reassign")
        assert sent["account_id"] == 12
        assert action.status == "success"
        assert action.result["original_account_id"] == 11
        assert action.result["reassigned_account_id"] == 12
        assert action.result["telegram_msg_id"] == "tg-group-reassigned"


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


def test_refresh_task_stats_archives_context_expired_skips_from_primary_counts():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        task = Task(id="task-archived-skip", tenant_id=1, name="活跃群", type="group_ai_chat", status="running", stats={})
        session.add(task)
        session.add_all(
            [
                Action(
                    id="send-success",
                    tenant_id=1,
                    task_id=task.id,
                    task_type="group_ai_chat",
                    action_type="send_message",
                    status="success",
                    scheduled_at=now_value,
                    executed_at=now_value,
                ),
                Action(
                    id="send-context-expired",
                    tenant_id=1,
                    task_id=task.id,
                    task_type="group_ai_chat",
                    action_type="send_message",
                    status="skipped",
                    scheduled_at=now_value,
                    executed_at=now_value,
                    result={"error_code": "context_expired"},
                ),
                Action(
                    id="send-real-skip",
                    tenant_id=1,
                    task_id=task.id,
                    task_type="group_ai_chat",
                    action_type="send_message",
                    status="skipped",
                    scheduled_at=now_value,
                    executed_at=now_value,
                    result={"error_code": "keyword_filtered"},
                ),
                Action(
                    id="membership-context-expired",
                    tenant_id=1,
                    task_id=task.id,
                    task_type="group_ai_chat",
                    action_type="ensure_target_membership",
                    status="skipped",
                    scheduled_at=now_value,
                    executed_at=now_value,
                    result={"error_code": "context_expired"},
                ),
            ]
        )
        session.commit()

        stats = refresh_task_stats(session, task)

        assert stats["success_count"] == 1
        assert stats["skipped_count"] == 1
        assert stats["total_actions"] == 2
        assert stats["archived_skipped_count"] == 1
        assert stats["raw_skipped_count"] == 2


def _add_cycle_skip_basics(session: Session, now_value: datetime) -> None:
    session.add(Tenant(id=1, name="默认运营空间"))
    session.add(
        Task(
            id="task-cycle-skip",
            tenant_id=1,
            name="skip",
            type="group_ai_chat",
            status="running",
            next_run_at=now_value + timedelta(hours=1),
        )
    )
    session.add(
        TgAccount(
            id=11,
            tenant_id=1,
            display_name="账号",
            phone_masked="+861***0011",
            status="在线",
            session_ciphertext="session",
        )
    )
    session.add(
        TgGroup(
            id=7,
            tenant_id=1,
            tg_peer_id="-1007",
            title="运营群",
            auth_status="已授权运营",
            can_send=True,
            require_review=False,
        )
    )
    session.add(TgGroupAccount(tenant_id=1, group_id=7, account_id=11, can_send=True))


def _add_cycle_contexts(session: Session, now_value: datetime) -> tuple[GroupContextMessage, GroupContextMessage]:
    old_context = GroupContextMessage(
        tenant_id=1,
        group_id=7,
        listener_account_id=11,
        content="旧上下文",
        remote_message_id="old",
        created_at=now_value - timedelta(minutes=2),
    )
    new_context = GroupContextMessage(
        tenant_id=1,
        group_id=7,
        listener_account_id=11,
        content="新上下文",
        remote_message_id="new",
        created_at=now_value,
    )
    session.add_all([old_context, new_context])
    session.flush()
    return old_context, new_context


def _cycle_action(action_id: str, scheduled_at: datetime, payload: dict) -> Action:
    return Action(
        id=action_id,
        tenant_id=1,
        task_id="task-cycle-skip",
        task_type="group_ai_chat",
        action_type="send_message",
        account_id=11,
        status="pending",
        scheduled_at=scheduled_at,
        payload=payload,
    )


def _expired_cycle_payload(
    context_id: int,
    *,
    cycle_id: str = "cycle-stale",
    text: str = "skip",
) -> dict:
    return {
        "group_id": 7,
        "message_text": text,
        "review_approved": True,
        "cycle_id": cycle_id,
        "context_snapshot_message_id": context_id,
        "context_expire_after_messages": 1,
    }


def test_context_expired_skip_clears_same_cycle_pending_actions(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()

    with Session(engine) as session:
        _add_cycle_skip_basics(session, now_value)
        old_context, new_context = _add_cycle_contexts(session, now_value)
        expired_payload = _expired_cycle_payload(old_context.id)
        session.add_all(
            [
                _cycle_action("action-stale-due", now_value, expired_payload),
                _cycle_action(
                    "action-stale-future",
                    now_value + timedelta(hours=1),
                    _expired_cycle_payload(old_context.id, text="future"),
                ),
                _cycle_action(
                    "action-fresh-future",
                    now_value + timedelta(hours=1),
                    _expired_cycle_payload(new_context.id, cycle_id="cycle-fresh"),
                ),
            ]
        )
        session.commit()
        monkeypatch.setattr(
            dispatcher,
            "credentials_for_account",
            lambda *args, **kwargs: object(),
        )
        monkeypatch.setattr(
            dispatcher.gateway,
            "send_message",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("context expired action must not call TG")
            ),
        )

        [claimed] = claim_actions(session, limit=1, worker_id="worker-test")

        assert dispatcher.dispatch_action(session, claimed) is True

        stale_due = session.get(Action, "action-stale-due")
        stale_future = session.get(Action, "action-stale-future")
        fresh_future = session.get(Action, "action-fresh-future")
        task = session.get(Task, "task-cycle-skip")
        assert stale_due.status == "skipped"
        assert stale_future.status == "skipped"
        assert stale_future.result["error_code"] == "context_expired"
        assert fresh_future.status == "pending"
        assert task.next_run_at < now_value + timedelta(minutes=5)


def test_context_expiration_ignores_backfilled_older_messages(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()

    with Session(engine) as session:
        _add_cycle_skip_basics(session, now_value)
        snapshot = GroupContextMessage(
            tenant_id=1,
            group_id=7,
            listener_account_id=11,
            content="当前快照",
            remote_message_id="snapshot",
            sent_at=now_value - timedelta(minutes=1),
            created_at=now_value,
        )
        session.add(snapshot)
        session.flush()
        backfilled_old = GroupContextMessage(
            tenant_id=1,
            group_id=7,
            listener_account_id=11,
            content="补录旧消息",
            remote_message_id="backfilled-old",
            sent_at=now_value - timedelta(minutes=30),
            created_at=now_value + timedelta(seconds=1),
        )
        session.add(backfilled_old)
        session.add(_cycle_action("action-backfill", now_value, _expired_cycle_payload(snapshot.id, cycle_id="cycle-backfill", text="should send")))
        session.commit()
        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *args, **kwargs: object())
        monkeypatch.setattr(dispatcher.gateway, "send_message", lambda *args, **kwargs: SendResult(True, remote_message_id="tg-context-ok"))

        [claimed] = claim_actions(session, limit=1, worker_id="worker-test")

        assert dispatcher.dispatch_action(session, claimed) is True

        refreshed = session.get(Action, "action-backfill")
        assert refreshed.status == "success"
        assert refreshed.result["telegram_msg_id"] == "tg-context-ok"


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


def test_group_permission_denied_marks_group_account_not_sendable(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(Task(id="task-permission", tenant_id=1, name="permission", type="group_ai_chat", status="running"))
        session.add(TgAccount(id=11, tenant_id=1, display_name="账号", phone_masked="+861***0011", status="在线", session_ciphertext="session"))
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="运营群", auth_status="已授权运营", can_send=True, require_review=False))
        session.add(TgGroupAccount(tenant_id=1, group_id=7, account_id=11, can_send=True, permission_label="已加入"))
        session.add(
            Action(
                id="action-permission",
                tenant_id=1,
                task_id="task-permission",
                task_type="group_ai_chat",
                action_type="send_message",
                account_id=11,
                status="pending",
                scheduled_at=now_value,
                payload={"group_id": 7, "message_text": "hello", "review_approved": True},
            )
        )
        session.commit()

        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *args, **kwargs: object())
        monkeypatch.setattr(
            dispatcher.gateway,
            "send_message",
            lambda *args, **kwargs: SendResult(False, failure_type="群无权限", detail="群无权限或账号不可发言"),
        )

        [claimed] = claim_actions(session, limit=1, worker_id="worker-test")
        assert dispatcher.dispatch_action(session, claimed) is True

        link = session.scalar(select(TgGroupAccount).where(TgGroupAccount.group_id == 7, TgGroupAccount.account_id == 11))
        assert link is not None
        assert link.can_send is False
        assert link.permission_label == "群无权限或账号不可发言"


def test_target_membership_requires_send_rechecks_existing_group_link(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(Task(id="task-membership", tenant_id=1, name="membership", type="group_ai_chat", status="running"))
        session.add(OperationTarget(id=21, tenant_id=1, target_type="group", tg_peer_id="-10021", title="目标群", auth_status="已授权运营", can_send=True))
        session.add(TgAccount(id=11, tenant_id=1, display_name="账号", phone_masked="+861***0011", status="在线", session_ciphertext="session"))
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-10021", title="目标群", auth_status="已授权运营", can_send=True, require_review=False))
        session.add(TgGroupAccount(tenant_id=1, group_id=7, account_id=11, can_send=True, permission_label="已加入"))
        session.add(
            Action(
                id="action-membership",
                tenant_id=1,
                task_id="task-membership",
                task_type="group_ai_chat",
                action_type="ensure_target_membership",
                account_id=11,
                status="pending",
                scheduled_at=now_value,
                payload={"channel_id": "-10021", "channel_target_id": 21, "target_type": "group", "target_display": "目标群", "require_send": True},
            )
        )
        session.commit()

        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *args, **kwargs: object())
        monkeypatch.setattr(dispatcher.gateway, "ensure_channel_membership", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("existing group link must be probed before join")))
        monkeypatch.setattr(
            dispatcher.gateway,
            "probe_target_capabilities",
            lambda *args, **kwargs: OperationResult(False, "失败", "群无权限", "缓存频道不可访问 / 账号无权限"),
        )

        action = session.get(Action, "action-membership")
        assert dispatcher.dispatch_action(session, action) is True

        link = session.scalar(select(TgGroupAccount).where(TgGroupAccount.group_id == 7, TgGroupAccount.account_id == 11))
        assert link is not None
        assert link.can_send is False
        assert link.permission_label == "缓存频道不可访问 / 账号无权限"
        assert action.status == "skipped"
        assert action.result["error_code"] == "membership_permission_denied"
        assert action.result["membership_status"] == "permission_denied"
        verification = session.scalar(select(VerificationTask).where(VerificationTask.group_id == 7, VerificationTask.account_id == 11))
        assert verification is not None
        assert verification.status == "待处理"
        assert verification.suggested_action == "人工处理"


def test_target_membership_follows_linked_channel_before_blocking_group_send(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(Task(id="task-linked-channel", tenant_id=1, name="linked", type="group_ai_chat", status="running"))
        session.add(OperationTarget(id=21, tenant_id=1, target_type="group", tg_peer_id="-10021", title="目标群", auth_status="已授权运营", can_send=True))
        session.add(TgAccount(id=11, tenant_id=1, display_name="账号", phone_masked="+861***0011", status="在线", session_ciphertext="session"))
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-10021", title="目标群", auth_status="已授权运营", can_send=True, require_review=False))
        session.add(TgGroupAccount(tenant_id=1, group_id=7, account_id=11, can_send=True, permission_label="已加入"))
        session.add(
            Action(
                id="action-linked-channel",
                tenant_id=1,
                task_id="task-linked-channel",
                task_type="group_ai_chat",
                action_type="ensure_target_membership",
                account_id=11,
                status="pending",
                scheduled_at=now_value,
                payload={"channel_id": "-10021", "channel_target_id": 21, "target_type": "group", "target_display": "目标群", "require_send": True},
            )
        )
        session.commit()

        probe_results = [
            OperationResult(False, "失败", "群无权限", "账号未关注/未加入目标频道或无法进入关联讨论区"),
            OperationResult(True, detail="group:-10021:可访问"),
        ]
        followed: list[tuple[int, str]] = []
        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *args, **kwargs: object())
        monkeypatch.setattr(dispatcher.gateway, "ensure_channel_membership", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("group was already joined")))
        monkeypatch.setattr(dispatcher.gateway, "probe_target_capabilities", lambda *args, **kwargs: probe_results.pop(0))
        monkeypatch.setattr(
            dispatcher.gateway,
            "ensure_linked_channel_membership",
            lambda account_id, target_peer_id, *args, **kwargs: followed.append((account_id, target_peer_id)) or OperationResult(True, "已处理", detail="已关注关联频道"),
            raising=False,
        )

        action = session.get(Action, "action-linked-channel")
        assert dispatcher.dispatch_action(session, action) is True

        link = session.scalar(select(TgGroupAccount).where(TgGroupAccount.group_id == 7, TgGroupAccount.account_id == 11))
        assert followed == [(11, "-10021")]
        assert link is not None and link.can_send is True
        assert action.status == "success"
        assert action.result["membership_status"] == "already_joined"
        assert session.scalar(select(VerificationTask).where(VerificationTask.group_id == 7)) is None


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

from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from app.database import Base
from app.integrations.telegram import OperationResult, SendResult
from app.models import (
    Action,
    AiAccountGroupStanceMemory,
    AiAccountVoiceProfile,
    AiGroupMessageMemory,
    DailyRuntimeStat,
    ExecutionAttempt,
    FailureType,
    GroupContextMessage,
    OperationTarget,
    ReviewQueue,
    RuntimeCleanupAudit,
    SchedulingSetting,
    Task,
    TaskAccountDailyCoverage,
    Tenant,
    TenantAiSetting,
    TenantLearningProfile,
    TgAccount,
    TgAccountOnlineState,
    TgGroup,
    TgGroupAccount,
    VerificationTask,
)
from app.schemas.task_center import TaskRetryRequest
from app.services._common import _now
from app.services.account_capacity import AccountCapacityCache, available_accounts_by_capacity
from app.services.task_center import dispatcher
from app.services.task_center.ai_generation_dependencies import GenerationDependencies
from app.services.task_center.ai_generator import GeneratedContent
from app.services.task_center import payloads as task_payloads
from app.services.task_center.executors import group_ai_chat
from app.services.task_center import service as task_service
from app.services.task_center import account_pool
from app.services.task_center.dispatcher import claim_actions
from app.services.task_center.runtime_retention import cleanup_runtime_details
from app.services.task_center.service import _recover_stale_executing_actions, retry_task
from app.services.task_center.stats import planner_backlog_snapshot, refresh_task_stats, retry_failed_actions
from app.timezone import BEIJING_TZ
from tests.capacity_ai_planner_test_support import (
    AiPlannerScenario,
    seed_ai_planner_scope,
    seed_sent_memory,
)
from tests.capacity_ai_dispatch_test_support import (
    assert_claimed_generation_batch,
    assert_quality_retry_states,
    configure_duplicate_generation,
    configure_pending_generation,
    pending_generation_cycle_batch,
    seed_duplicate_generation_scope,
    seed_pending_generation_scope,
)


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


def _voice_profile(account_id: int, summary: str = "青年短句，少总结，偶尔追问") -> AiAccountVoiceProfile:
    return AiAccountVoiceProfile(
        tenant_id=1,
        account_id=account_id,
        version=1,
        status="active",
        quality_status="active",
        short_prompt_summary=summary,
    )


def _forbid_planner_ai_generation(monkeypatch) -> None:
    def fail(*_args, **_kwargs):
        pytest.fail("planner phase must not call AI generation")

    monkeypatch.setattr("app.services.task_center.ai_generator.generate_group_messages", fail)
    monkeypatch.setattr("app.services.task_center.ai_generator.generate_group_reply_messages", fail)


def _slot_bound_contents(config: dict, contents: list[str]) -> list[GeneratedContent]:
    return [
        GeneratedContent(
            content,
            slot_id=slot["slot_id"],
            sequence_index=index,
        )
        for index, (slot, content) in enumerate(
            zip(config["generation_slots"], contents, strict=True),
            1,
        )
    ]


def _dispatch_planned_ai_actions(
    session: Session,
    monkeypatch,
    actions: list[Action],
    *,
    normal_generator,
) -> list[Action]:
    for action in actions:
        action.status = "executing"
        action.claim_owner = "capacity-dispatch-test"
        action.claim_token = "capacity-dispatch-claim"
        action.payload = {
            **(action.payload or {}),
            "ai_generation_claim_owner": action.claim_owner,
            "ai_generation_claim_token": action.claim_token,
        }
    session.commit()
    monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(dispatcher, "is_account_online_ready", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        dispatcher.gateway,
        "send_message",
        lambda *_args, **_kwargs: SendResult(True, remote_message_id="capacity-ai-ok"),
    )
    def forbidden_reply(*_args, **_kwargs):
        pytest.fail("normal action must not use reply generation")

    dependencies = GenerationDependencies(
        normal_generator=normal_generator,
        reply_generator=forbidden_reply,
        reply_target_probe=forbidden_reply,
        reply_messages_fetcher=forbidden_reply,
    )
    for action in actions:
        if action.status == "executing":
            dispatcher.dispatch_action(session, action, generation_dependencies=dependencies)
    return actions


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
    now_value = datetime(2026, 6, 27, 12, 0, 0)

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


@pytest.mark.no_postgres
def test_claim_actions_takes_one_group_rescue_invite_per_admin_batch():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间", group_rescue_enabled=True, group_rescue_admin_account_id=515))
        session.add(TgAccount(id=515, tenant_id=1, display_name="管理员", phone_masked="+195***0433", status="在线"))
        session.add(Task(id="task-rescue-claim", tenant_id=1, name="rescue", type="target_admission_retry", status="running", priority=1))
        session.add_all(
            [
                Action(id="rescue-1", tenant_id=1, task_id="task-rescue-claim", task_type="target_admission_retry", action_type="invite_group_account", account_id=515, status="pending", scheduled_at=now_value, payload={"group_id": 7, "group_peer_id": "-1007", "target_account_id": 11, "target_account_ref": "@target_11", "trigger_account_id": 11}),
                Action(id="rescue-2", tenant_id=1, task_id="task-rescue-claim", task_type="target_admission_retry", action_type="invite_group_account", account_id=515, status="pending", scheduled_at=now_value, payload={"group_id": 7, "group_peer_id": "-1007", "target_account_id": 12, "target_account_ref": "@target_12", "trigger_account_id": 12}),
            ]
        )
        session.commit()

        claimed = claim_actions(session, limit=2, worker_id="worker-test")

        assert [action.id for action in claimed] == ["rescue-1"]
        untouched = session.get(Action, "rescue-2")
        assert untouched.status == "pending"
        assert untouched.result in ({}, None)


@pytest.mark.no_postgres
def test_claim_actions_prioritizes_target_admission_retry_over_overdue_group_send():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()

    with Session(engine) as session:
        session.add_all([
            Tenant(id=1, name="默认运营空间"),
            TgAccount(id=11, tenant_id=1, display_name="准入账号", phone_masked="+195***0011", status="在线"),
            TgAccount(id=12, tenant_id=1, display_name="活群账号", phone_masked="+195***0012", status="在线"),
            Task(id="task-admission", tenant_id=1, name="准入重试", type="target_admission_retry", status="running", priority=3),
            Task(id="task-ai", tenant_id=1, name="AI 活群", type="group_ai_chat", status="running", priority=3),
            Action(id="admission", tenant_id=1, task_id="task-admission", task_type="target_admission_retry", action_type="ensure_target_membership", account_id=11, status="pending", scheduled_at=now_value - timedelta(seconds=10)),
            Action(id="ai-send", tenant_id=1, task_id="task-ai", task_type="group_ai_chat", action_type="send_message", account_id=12, status="pending", scheduled_at=now_value - timedelta(seconds=20), payload={"message_text": "排队消息"}),
        ])
        session.commit()

        claimed = claim_actions(session, limit=1, worker_id="worker-test")

    assert [action.id for action in claimed] == ["admission"]


@pytest.mark.no_postgres
def test_claim_actions_backs_off_group_rescue_inflight_conflict(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()
    settings = _redis_bucket_settings(enable_redis_account_inflight=True)
    monkeypatch.setattr(dispatcher, "get_settings", lambda: settings)
    monkeypatch.setattr(dispatcher, "_redis_client", lambda _redis_url: FakeRedisAccountLock(locked=False))

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间", group_rescue_enabled=True, group_rescue_admin_account_id=515))
        session.add(TgAccount(id=515, tenant_id=1, display_name="管理员", phone_masked="+195***0433", status="在线"))
        session.add(Task(id="task-rescue-lock", tenant_id=1, name="rescue", type="target_admission_retry", status="running", priority=1))
        session.add(Action(id="rescue-lock", tenant_id=1, task_id="task-rescue-lock", task_type="target_admission_retry", action_type="invite_group_account", account_id=515, status="pending", scheduled_at=now_value, payload={"group_id": 7, "group_peer_id": "-1007", "target_account_id": 11, "target_account_ref": "@target_11", "trigger_account_id": 11}))
        session.commit()

        claimed = claim_actions(session, limit=1, worker_id="worker-test")

        action = session.get(Action, "rescue-lock")
        assert claimed == []
        assert action.status == "pending"
        assert action.result["claim_released_reason"] == "account_inflight_conflict"
        assert action.scheduled_at >= now_value + timedelta(seconds=dispatcher.GROUP_RESCUE_INFLIGHT_CONFLICT_BACKOFF_SECONDS)


@pytest.mark.no_postgres
def test_claim_actions_skips_resolved_group_rescue_invite_before_runtime_reservation(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间", group_rescue_enabled=True, group_rescue_admin_account_id=515))
        session.add(TgAccount(id=11, tenant_id=1, display_name="目标账号", phone_masked="+195***0011", status="在线"))
        session.add(TgAccount(id=515, tenant_id=1, display_name="管理员", phone_masked="+195***0433", status="在线"))
        session.add(TgGroup(id=7, tenant_id=1, title="天津音乐学院", tg_peer_id="-1007"))
        session.add(TgGroupAccount(id=1, tenant_id=1, group_id=7, account_id=11, can_send=True, permission_label="可发言"))
        session.add(Task(id="task-rescue-stale", tenant_id=1, name="rescue", type="target_admission_retry", status="running", priority=1))
        session.add(
            Action(
                id="rescue-stale",
                tenant_id=1,
                task_id="task-rescue-stale",
                task_type="target_admission_retry",
                action_type="invite_group_account",
                account_id=515,
                status="pending",
                scheduled_at=now_value,
                payload={
                    "group_id": 7,
                    "group_peer_id": "-1007",
                    "target_account_id": 11,
                    "target_account_ref": "@target_11",
                    "trigger_account_id": 11,
                },
            )
        )
        session.commit()

        def fail_reservation(_action):
            raise AssertionError("resolved invite should not reserve runtime resources")

        monkeypatch.setattr(dispatcher, "_reserve_runtime_resources", fail_reservation)

        claimed = claim_actions(session, limit=1, worker_id="worker-test")

        action = session.get(Action, "rescue-stale")
        assert claimed == []
        assert action.status == "skipped"
        assert action.result["error_code"] == "admission_retry_target_already_joined"
        assert action.result["rescue_status"] == "already_joined_skipped"


@pytest.mark.no_postgres
def test_release_runtime_resources_keeps_later_holder_inflight() -> None:
    old_action = Action(id="old-action", account_id=11)
    dispatcher._IN_FLIGHT_ACCOUNTS.add(11)
    dispatcher._ACTION_RESERVATIONS["new-action"] = dispatcher._runtime_resources._RuntimeReservation(account_id=11)

    dispatcher._release_runtime_resources(old_action)

    assert 11 in dispatcher._IN_FLIGHT_ACCOUNTS
    assert "new-action" in dispatcher._ACTION_RESERVATIONS


@pytest.mark.no_postgres
def test_dispatch_action_always_releases_runtime_resources(monkeypatch) -> None:
    action = Action(
        id="action-finally-release",
        tenant_id=1,
        task_id="task-finally-release",
        task_type="group_ai_chat",
        action_type="send_message",
        account_id=11,
        payload={},
    )
    dispatcher._IN_FLIGHT_ACCOUNTS.add(11)
    dispatcher._ACTION_RESERVATIONS[action.id] = dispatcher._runtime_resources._RuntimeReservation(
        account_id=11,
    )
    monkeypatch.setattr(dispatcher, "_dispatch_action", lambda *_args, **_kwargs: True)

    assert dispatcher.dispatch_action(object(), action) is True

    assert 11 not in dispatcher._IN_FLIGHT_ACCOUNTS
    assert action.id not in dispatcher._ACTION_RESERVATIONS


@pytest.mark.no_postgres
def test_action_dedupe_key_ignores_dynamic_generation_metadata() -> None:
    task = Task(id="task-dedupe", tenant_id=1, stats={"current_plan_batch_key": "batch-1"})
    payload = {
        "group_id": 7,
        "message_text": "同一条业务发言",
        "cycle_id": "cycle-a",
        "turn_index": 2,
        "ai_generation_id": "gen-a",
        "ai_generation_tokens": 128,
        "ai_generation_history": "draft-a",
        "context_message_ids": [1, 2, 3],
        "context_snapshot_message_id": 3,
    }
    changed_dynamic = {
        **payload,
        "ai_generation_id": "gen-b",
        "ai_generation_tokens": 256,
        "ai_generation_history": "draft-b",
        "context_message_ids": [2, 3, 4],
        "context_snapshot_message_id": 4,
    }
    changed_business = {**changed_dynamic, "message_text": "另一条业务发言"}

    first_key = task_payloads._action_dedupe_key(task, "batch-1", "send_message", 11, payload)

    assert task_payloads._action_dedupe_key(task, "batch-1", "send_message", 11, changed_dynamic) == first_key
    assert task_payloads._action_dedupe_key(task, "batch-1", "send_message", 11, changed_business) != first_key


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


@pytest.mark.no_postgres
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
        gate_payload = _add_group_ai_send_gate_payload(
            session,
            now_value,
            action_id="action-group-reassign",
            task_id="task-group-reassign",
            group_id=7,
            account_id=12,
            text="new",
        )
        session.add(Action(id="action-group-reassign", tenant_id=1, task_id="task-group-reassign", task_type="group_ai_chat", action_type="send_message", account_id=11, status="pending", scheduled_at=now_value, payload={"group_id": 7, "message_text": "new", "review_approved": True, **gate_payload}))
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


def _seed_coverage_no_reassign_scope(session: Session) -> tuple[Action, TgAccount]:
    session.add(Tenant(id=1, name="默认运营空间"))
    account = TgAccount(
        id=11, tenant_id=1, display_name="覆盖账号A", phone_masked="+861***0011",
        status="在线", session_ciphertext="session-a",
    )
    session.add_all([
        account,
        TgAccount(
            id=12, tenant_id=1, display_name="账号B", phone_masked="+861***0012",
            status="在线", session_ciphertext="session-b",
        ),
        TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="运营群"),
        Task(
            id="task-coverage-no-reassign", tenant_id=1, name="每日覆盖",
            type="group_ai_chat", status="running", account_config={"selection_mode": "all"},
        ),
    ])
    session.flush()
    session.add_all([
        TgGroupAccount(tenant_id=1, group_id=7, account_id=11, can_send=False),
        TgGroupAccount(tenant_id=1, group_id=7, account_id=12, can_send=True),
    ])
    action = Action(
        id="coverage-no-reassign", tenant_id=1, task_id="task-coverage-no-reassign",
        task_type="group_ai_chat", action_type="send_message", account_id=11,
        status="claiming", scheduled_at=_now(),
        payload={
            "group_id": 7, "message_text": "覆盖消息", "coverage_ledger_id": "coverage-row",
            "account_coverage_mode": "all_accounts_daily",
        },
    )
    session.add(action)
    session.flush()
    return action, account


@pytest.mark.no_postgres
def test_coverage_action_does_not_reassign_when_account_lost_permission():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        action, account = _seed_coverage_no_reassign_scope(session)

        replacement = dispatcher._replacement_for_lost_group_send_permission(session, action, account)

        assert replacement is None
        assert action.account_id == 11


def test_dispatch_global_policy_excludes_current_executing_hard_hourly_action(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()
    sent: dict[str, int] = {}

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(SchedulingSetting(tenant_id=1, default_account_hour_limit=1, jitter_min_seconds=0, jitter_max_seconds=0))
        session.add(TgAccount(id=11, tenant_id=1, display_name="账号A", phone_masked="+861***0011", status="在线", session_ciphertext="session-a"))
        session.add(TgAccount(id=12, tenant_id=1, display_name="账号B", phone_masked="+861***0012", status="在线", session_ciphertext="session-b"))
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="运营群", auth_status="已授权运营", can_send=True, require_review=False))
        session.add(TgGroupAccount(tenant_id=1, group_id=7, account_id=11, can_send=True, permission_label="可发言"))
        session.add(TgGroupAccount(tenant_id=1, group_id=7, account_id=12, can_send=True, permission_label="可发言"))
        session.add(Task(id="task-hard-hourly", tenant_id=1, name="硬目标", type="group_ai_chat", status="running", priority=1))
        gate_payload = _add_group_ai_send_gate_payload(
            session,
            now_value,
            action_id="action-hard-hourly",
            task_id="task-hard-hourly",
            group_id=7,
            account_id=11,
            text="new",
        )
        session.add(
            Action(
                id="action-prior-hourly",
                tenant_id=1,
                task_id="task-hard-hourly",
                task_type="group_ai_chat",
                action_type="send_message",
                account_id=11,
                status="success",
                scheduled_at=now_value - timedelta(minutes=1),
                executed_at=now_value - timedelta(minutes=1),
                payload={"group_id": 7, "message_text": "old"},
            )
        )
        session.add(
            Action(
                id="action-hard-hourly",
                tenant_id=1,
                task_id="task-hard-hourly",
                task_type="group_ai_chat",
                action_type="send_message",
                account_id=11,
                status="pending",
                scheduled_at=_now(),
                payload={"group_id": 7, "message_text": "new", "review_approved": True, "hard_hourly_target": True, **gate_payload},
            )
        )
        session.commit()

        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *args, **kwargs: object())

        def fake_send_message(account_id, *_args, **_kwargs):  # noqa: ANN001
            sent["account_id"] = account_id
            return SendResult(True, remote_message_id="tg-hard-hourly")

        monkeypatch.setattr(dispatcher.gateway, "send_message", fake_send_message)

        [claimed] = claim_actions(session, limit=1, worker_id="worker-test")

        assert claimed.status == "executing"
        assert dispatcher.dispatch_action(session, claimed) is True

        action = session.get(Action, "action-hard-hourly")
        assert sent["account_id"] == 11
        assert action.status == "success"
        assert action.result["account_policy_action"] == "hard_hourly_capacity_override"
        assert action.result["account_policy_reason"] == "hard_hourly_target"
        assert action.result["telegram_msg_id"] == "tg-hard-hourly"


@pytest.mark.no_postgres
@pytest.mark.parametrize(("claim_limit", "expected_generation_count"), [(1, 1), (2, 2)])
def test_dispatch_hard_hourly_generates_pending_ai_message_before_send(
    monkeypatch,
    claim_limit: int,
    expected_generation_count: int,
):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()
    generated: dict[str, object] = {}
    sent: dict[str, object] = {}
    with Session(engine) as session:
        seed_pending_generation_scope(session, now_value, claim_limit)
        dependencies = configure_pending_generation(monkeypatch, generated, sent)
        claimed = claim_actions(session, limit=claim_limit, worker_id="worker-test")
        assert_claimed_generation_batch(session, claimed, expected_generation_count)
        assert dispatcher.dispatch_action(
            session,
            claimed[0],
            generation_dependencies=dependencies,
        ) is True
        action = session.get(Action, "action-hard-hourly-ai")
        sibling = session.get(Action, "action-hard-hourly-ai-sibling")
        assert generated == {
            "model": "mino-v2.5",
            "count": expected_generation_count,
            "target": "运营群",
            "history": "真人: 今天怎么安排",
            "personas": {"11": "活跃群友", **({"12": "追问群友"} if expected_generation_count == 2 else {})},
        }
        assert sent == {"account_id": 11, "content": "今天先看看群公告"}
        assert action.payload["message_text"] == "今天先看看群公告"
        assert sibling.payload["message_text"] == ("第二条我也等等看" if expected_generation_count == 2 else "")
        assert action.payload["ai_generation_status"] == "ready"
        assert sibling.payload["ai_generation_status"] == ("ready" if expected_generation_count == 2 else "pending")
        assert action.payload["ai_message_memory_id"]
        assert bool(sibling.payload.get("ai_message_memory_id")) is (expected_generation_count == 2)
        assert action.payload["ai_generation_tokens"] == 17
        assert action.status == "success"
        action_memory = session.get(AiGroupMessageMemory, action.payload["ai_message_memory_id"])
        assert action_memory is not None
        assert action_memory.action_id == action.id
        assert action_memory.status == "success"
        if expected_generation_count == 2:
            sibling_memory = session.get(AiGroupMessageMemory, sibling.payload["ai_message_memory_id"])
            assert sibling_memory is not None
            assert sibling_memory.action_id == sibling.id
            assert sibling_memory.status == "reserved"


@pytest.mark.no_postgres
def test_dispatch_hard_hourly_pending_ai_duplicate_is_blocked(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        action, coverage = seed_duplicate_generation_scope(session, _now())
        dependencies = configure_duplicate_generation(monkeypatch)
        [claimed] = claim_actions(session, limit=1, worker_id="worker-test")
        assert dispatcher.dispatch_action(
            session,
            claimed,
            generation_dependencies=dependencies,
        ) is True
        session.refresh(action)
        assert action.status == "failed"
        assert action.result["error_code"] == "duplicate_message"
        assert action.result["validation_stage"] == "ai_message_memory"
        assert action.payload["quality_skip_reason"] == "duplicate_message"
        assert coverage.state == "ready"
        assert coverage.reserved_action_id is None


@pytest.mark.no_postgres
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


def _add_cycle_ai_send_gate_state(
    session: Session,
    now_value: datetime,
    *,
    memory_id: str,
    text: str,
) -> dict:
    session.add(
        TgAccountOnlineState(
            tenant_id=1,
            account_id=11,
            desired_online=True,
            online_status="online",
            stale_after_at=now_value + timedelta(minutes=1),
        )
    )
    session.add(
        AiGroupMessageMemory(
            id=memory_id,
            tenant_id=1,
            group_id=7,
            task_id="task-cycle-skip",
            account_id=11,
            raw_text=text,
            normalized_text=text,
            text_fingerprint=memory_id,
            status="reserved",
            planned_at=now_value,
        )
    )
    return {
        "slot_id": f"task-cycle-skip:cycle:1:turn:{memory_id}",
        "ai_message_memory_id": memory_id,
    }


def _add_group_ai_send_gate_payload(
    session: Session,
    now_value: datetime,
    *,
    action_id: str,
    task_id: str,
    group_id: int,
    account_id: int,
    text: str,
) -> dict:
    if not session.scalar(select(TgAccountOnlineState).where(TgAccountOnlineState.tenant_id == 1, TgAccountOnlineState.account_id == account_id)):
        session.add(
            TgAccountOnlineState(
                tenant_id=1,
                account_id=account_id,
                desired_online=True,
                online_status="online",
                stale_after_at=now_value + timedelta(minutes=5),
            )
        )
    memory_id = f"memory-{action_id}"
    session.add(
        AiGroupMessageMemory(
            id=memory_id,
            tenant_id=1,
            group_id=group_id,
            task_id=task_id,
            account_id=account_id,
            raw_text=text,
            normalized_text=text,
            text_fingerprint=memory_id,
            status="reserved",
            planned_at=now_value,
        )
    )
    return {"slot_id": f"{task_id}:cycle:test:turn:{action_id}", "ai_message_memory_id": memory_id}


def _add_group_ai_send_action_with_online_state(
    session: Session,
    now_value: datetime,
    *,
    online_status: str,
    action_id: str,
    memory_id: str,
    text: str,
) -> None:
    session.add(
        TgAccountOnlineState(
            tenant_id=1,
            account_id=11,
            desired_online=True,
            online_status=online_status,
            stale_after_at=now_value + timedelta(minutes=1),
        )
    )
    session.add(
        AiGroupMessageMemory(
            id=memory_id,
            tenant_id=1,
            group_id=7,
            task_id="task-cycle-skip",
            account_id=11,
            raw_text=text,
            normalized_text=text,
            text_fingerprint=memory_id,
            status="reserved",
            planned_at=now_value,
        )
    )
    session.add(
        _cycle_action(
            action_id,
            now_value,
            {
                "group_id": 7,
                "message_text": text,
                "review_approved": True,
                "slot_id": "task-cycle-skip:cycle:1:turn:1",
                "ai_message_memory_id": memory_id,
            },
        )
    )


@pytest.mark.no_postgres
def test_task_detail_exposes_ai_quality_funnel_with_blocker_samples():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = datetime(2026, 6, 29, 10, 0, tzinfo=BEIJING_TZ)
    with Session(engine) as session:
        task = Task(
            id="task-ai-quality",
            tenant_id=1,
            name="质量漏斗",
            type="group_ai_chat",
            status="running",
            account_config={"selection_mode": "manual", "account_ids": [11, 12]},
            stats={
                "quality_rejection_counts": {"voice_profile_mismatch": 1},
                "quality_rejection_samples": [
                    {
                        "reason": "voice_profile_mismatch",
                        "content": "😀😀",
                        "status": "filtered",
                        "account_id": 11,
                        "detail": "账号面具要求少表情",
                    }
                ],
            },
        )
        online_now = _now()
        session.add(task)
        session.add_all(
            [
                TgAccountOnlineState(
                    tenant_id=1,
                    account_id=11,
                    desired_online=True,
                    desired_sources=[{"source_type": "task", "source_id": task.id}],
                    online_status="online",
                    stale_after_at=online_now + timedelta(minutes=1),
                ),
                TgAccountOnlineState(
                    tenant_id=1,
                    account_id=12,
                    desired_online=True,
                    desired_sources=[{"source_type": "task", "source_id": task.id}],
                    online_status="online",
                    stale_after_at=online_now - timedelta(seconds=1),
                    failure_type="stale_probe",
                ),
                Action(
                    id="quality-ok",
                    tenant_id=1,
                    task_id=task.id,
                    task_type="group_ai_chat",
                    action_type="send_message",
                    account_id=11,
                    status="success",
                    scheduled_at=now_value,
                    executed_at=now_value,
                    payload={
                        "message_text": "花花这个服务我上次看反馈还行",
                        "cycle_id": "cycle-1",
                        "ai_generation_id": "cycle-1",
                        "ai_generation_count": 8,
                        "generation_source": "human_context",
                        "human_quality_decision": "accepted",
                        "act_type": "experience",
                        "account_voice_profile_version": 3,
                        "account_voice_profile_summary": "青年号，短句追问，少用表情",
                        "account_voice_profile_match_score": 86,
                        "account_voice_profile_match_reason": "voice_profile_matched",
                        "stance_summary": "前面认可花花服务，后续保持谨慎夸",
                        "ai_message_memory_id": "memory-quality-ok",
                        "semantic_cluster": "huahua_service_feedback",
                        "rule_trace": {
                            "material_intent": "表情包:围观",
                            "material_matched_tags": ["围观", "吃瓜"],
                            "material_candidate_count": 3,
                            "material_id": 9301,
                            "material_failure_reason": "",
                        },
                    },
                ),
                Action(
                    id="quality-duplicate",
                    tenant_id=1,
                    task_id=task.id,
                    task_type="group_ai_chat",
                    action_type="send_message",
                    account_id=12,
                    status="skipped",
                    scheduled_at=now_value,
                    payload={
                        "message_text": "这个确实不错",
                        "quality_skip_reason": "duplicate_message",
                        "duplicate_risk": "semantic_cluster",
                    },
                ),
                Action(
                    id="quality-template",
                    tenant_id=1,
                    task_id=task.id,
                    task_type="group_ai_chat",
                    action_type="send_message",
                    account_id=13,
                    status="skipped",
                    scheduled_at=now_value,
                    payload={
                        "message_text": "感觉挺靠谱",
                        "quality_skip_reason": "template_shell_limited",
                    },
                ),
                Action(
                    id="quality-offline",
                    tenant_id=1,
                    task_id=task.id,
                    task_type="group_ai_chat",
                    action_type="send_message",
                    account_id=14,
                    status="failed",
                    scheduled_at=now_value,
                    payload={"message_text": "我看看"},
                    result={"validation_stage": "account_online", "error_code": "account_offline"},
                ),
                Action(
                    id="quality-fallback",
                    tenant_id=1,
                    task_id=task.id,
                    task_type="group_ai_chat",
                    action_type="send_message",
                    account_id=15,
                    status="success",
                    scheduled_at=now_value,
                    payload={
                        "message_text": "👌",
                        "quality_fallback": "emoji_react",
                        "human_quality_decision": "quality_fallback",
                    },
                ),
                Action(
                    id="quality-profile-low",
                    tenant_id=1,
                    task_id=task.id,
                    task_type="group_ai_chat",
                    action_type="send_message",
                    account_id=16,
                    status="success",
                    scheduled_at=now_value,
                    executed_at=now_value,
                    payload={
                        "message_text": "先看看群里怎么说",
                        "human_quality_decision": "accepted",
                        "profile_match_score": 0,
                        "profile_match_reason": "profile_unavailable",
                        "profile_unavailable_reason": "画像样本不足",
                    },
                ),
            ]
        )
        session.commit()

        detail = task_service.get_task_detail(session, 1, task.id)

        funnel = detail["ai_quality_funnel"]
        assert funnel["totals"]["candidate_count"] == 8
        assert funnel["totals"]["passed_count"] == 2
        assert funnel["totals"]["final_send_count"] == 3
        assert funnel["reason_counts"]["duplicate_message"] == 1
        assert funnel["reason_counts"]["template_shell_limited"] == 1
        assert funnel["reason_counts"]["account_offline"] == 1
        assert funnel["reason_counts"]["quality_fallback"] == 1
        assert funnel["reason_counts"]["profile_low_match"] == 1
        assert funnel["reason_counts"]["voice_profile_mismatch"] == 1
        assert funnel["samples"]["duplicate_message"][0]["content"] == "这个确实不错"
        assert funnel["samples"]["profile_low_match"][0]["action_id"] == "quality-profile-low"
        assert funnel["samples"]["voice_profile_mismatch"][0]["content"] == "😀😀"
        assert funnel["samples"]["voice_profile_mismatch"][0]["detail"] == "账号面具要求少表情"
        assert detail["ai_generation_records"][0]["generation_source"] == "human_context"
        cycles, total_cycles = task_service.list_ai_cycles_page(session, 1, task.id)
        assert total_cycles == 1
        turn = cycles[0]["turns"][0]
        assert turn["generation_source"] == "human_context"
        assert turn["act_type"] == "detail_follow"
        assert turn["account_voice_profile_version"] == 3
        assert turn["account_voice_profile_summary"] == "青年号，短句追问，少用表情"
        assert turn["account_voice_profile_match_score"] == 86
        assert turn["account_voice_profile_match_reason"] == "voice_profile_matched"
        assert turn["account_mask_version"] == 3
        assert turn["account_mask_summary"] == "青年号，短句追问，少用表情"
        assert turn["account_mask_match_score"] == 86
        assert turn["account_mask_match_reason"] == "voice_profile_matched"
        assert turn["stance_summary"] == "前面认可花花服务，后续保持谨慎夸"
        assert turn["ai_message_memory_id"] == "memory-quality-ok"
        assert turn["semantic_cluster"] == "huahua_service_feedback"
        assert turn["material_intent"] == "表情包:围观"
        assert turn["material_matched_tags"] == ["围观", "吃瓜"]
        assert turn["material_candidate_count"] == 3
        assert turn["material_id"] == 9301
        assert turn["material_failure_reason"] == ""
        online = detail["account_online_summary"]
        assert online["desired_count"] == 2
        assert online["online_count"] == 1
        assert online["stale_count"] == 1


@pytest.mark.no_postgres
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


@pytest.mark.no_postgres
def test_context_expired_reply_keeps_same_cycle_hard_hourly_plain_send_pending():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()

    with Session(engine) as session:
        _add_cycle_skip_basics(session, now_value)
        old_context, _new_context = _add_cycle_contexts(session, now_value)
        expired_reply_payload = {
            **_expired_cycle_payload(old_context.id, text="expired reply"),
            "reply_to_message_id": 1001,
            "hard_hourly_target": True,
        }
        session.add_all(
            [
                _cycle_action("action-expired-reply", now_value, expired_reply_payload),
                _cycle_action(
                    "action-hard-hourly-plain",
                    now_value + timedelta(minutes=10),
                    {
                        **_expired_cycle_payload(old_context.id, text=""),
                        "hard_hourly_target": True,
                        "ai_generation_status": "pending",
                    },
                ),
                _cycle_action(
                    "action-ordinary-stale",
                    now_value + timedelta(minutes=20),
                    _expired_cycle_payload(old_context.id, text="ordinary stale"),
                ),
                _cycle_action(
                    "action-daily-coverage-deferred",
                    now_value + timedelta(minutes=30),
                    {
                        **_expired_cycle_payload(old_context.id, text=""),
                        "account_coverage_mode": "all_accounts_daily",
                        "ai_generation_status": "pending",
                    },
                ),
            ]
        )
        session.commit()

        current = session.get(Action, "action-expired-reply")
        payload = task_payloads.SendMessagePayload.model_validate(current.payload or {})
        dispatcher._skip_context_expired_cycle(session, current, payload)

        hard_hourly_plain = session.get(Action, "action-hard-hourly-plain")
        ordinary_stale = session.get(Action, "action-ordinary-stale")
        daily_coverage_deferred = session.get(Action, "action-daily-coverage-deferred")
        assert hard_hourly_plain.status == "pending"
        assert daily_coverage_deferred.status == "pending"
        assert ordinary_stale.status == "skipped"
        assert ordinary_stale.result["error_code"] == "context_expired"


@pytest.mark.no_postgres
def test_daily_coverage_deferred_generation_refreshes_latest_human_context():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()

    with Session(engine) as session:
        _add_cycle_skip_basics(session, now_value)
        old_context, new_context = _add_cycle_contexts(session, now_value)
        action = _cycle_action(
            "action-daily-context-refresh",
            now_value,
            {
                **_expired_cycle_payload(old_context.id, text=""),
                "account_coverage_mode": "all_accounts_daily",
                "ai_generation_status": "pending",
                "ai_generation_history": "真人用户: 旧上下文",
            },
        )
        session.add(action)
        session.commit()

        payload = task_payloads.SendMessagePayload.model_validate(action.payload or {})
        refreshed = dispatcher._ai_generation_dispatch._refresh_normal_context(
            session,
            session.get(Task, action.task_id),
            [(action, payload)],
        )

        refreshed_payload = refreshed[0][1]
        assert refreshed_payload.context_snapshot_message_id == new_context.id
        assert refreshed_payload.context_message_ids == [old_context.id, new_context.id]
        assert "旧上下文" in refreshed_payload.ai_generation_history
        assert "新上下文" in refreshed_payload.ai_generation_history


@pytest.mark.no_postgres
def test_daily_coverage_deferred_generation_batches_only_near_term_siblings():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()

    with Session(engine) as session:
        _add_cycle_skip_basics(session, now_value)
        payload = {
            "group_id": 7,
            "message_text": "",
            "account_coverage_mode": "all_accounts_daily",
            "ai_generation_id": "daily-near-term",
            "ai_generation_status": "pending",
        }
        session.add_all([
            _cycle_action("action-daily-current", now_value, payload),
            _cycle_action("action-daily-near", now_value + timedelta(minutes=1), payload),
            _cycle_action("action-daily-far", now_value + timedelta(minutes=10), payload),
        ])
        session.commit()

        current = session.get(Action, "action-daily-current")
        session.get(Action, "action-daily-near").account_id = 12
        session.get(Action, "action-daily-far").account_id = 13
        for row in session.query(Action).filter(Action.id.in_(["action-daily-current", "action-daily-near", "action-daily-far"])):
            row.status = "executing"
            row.claim_owner = "worker-a"
            row.claim_token = "claim-daily"
            row.payload = {
                **row.payload,
                "ai_generation_claim_owner": "worker-a",
                "ai_generation_claim_token": "claim-daily",
            }
        session.commit()
        current_payload = task_payloads.SendMessagePayload.model_validate(current.payload or {})
        batch = dispatcher._ai_generation_dispatch._pending_generation_batch(session, current, current_payload)

        assert [action.id for action, _payload in batch] == [
            "action-daily-current",
            "action-daily-near",
        ]


@pytest.mark.no_postgres
def test_group_ai_send_requires_online_state_before_gateway(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()

    with Session(engine) as session:
        _add_cycle_skip_basics(session, now_value)
        _add_group_ai_send_action_with_online_state(
            session,
            now_value,
            online_status="offline",
            action_id="action-offline-send",
            memory_id="memory-offline-send",
            text="这条不应该发送",
        )
        session.commit()
        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *args, **kwargs: object())
        monkeypatch.setattr(
            dispatcher.gateway,
            "send_message",
            lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("offline account must not call TG")),
        )

        [claimed] = claim_actions(session, limit=1, worker_id="worker-test")

        assert dispatcher.dispatch_action(session, claimed) is True
        action = session.get(Action, "action-offline-send")
        assert action.status == "failed"
        assert action.result["error_code"] == FailureType.ACCOUNT_UNAVAILABLE.value
        assert action.result["validation_stage"] == "account_online"
        assert "在线" in action.result["error_message"]
        memory = session.get(AiGroupMessageMemory, "memory-offline-send")
        assert memory.status == "account_offline"
        assert memory.action_id == "action-offline-send"


@pytest.mark.no_postgres
def test_group_ai_send_requires_ready_not_warming_before_gateway(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()

    with Session(engine) as session:
        _add_cycle_skip_basics(session, now_value)
        _add_group_ai_send_action_with_online_state(
            session,
            now_value,
            online_status="warming",
            action_id="action-warming-send",
            memory_id="memory-warming-send",
            text="预热中账号不应该发送",
        )
        session.commit()
        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *args, **kwargs: object())
        monkeypatch.setattr(
            dispatcher.gateway,
            "send_message",
            lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("warming account must not call TG")),
        )

        [claimed] = claim_actions(session, limit=1, worker_id="worker-test")

        assert dispatcher.dispatch_action(session, claimed) is True
        action = session.get(Action, "action-warming-send")
        assert action.status == "failed"
        assert action.result["validation_stage"] == "account_online"
        memory = session.get(AiGroupMessageMemory, "memory-warming-send")
        assert memory.status == "account_offline"


@pytest.mark.no_postgres
def test_group_ai_send_without_message_memory_is_blocked_before_gateway(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()

    with Session(engine) as session:
        _add_cycle_skip_basics(session, now_value)
        session.add(
            TgAccountOnlineState(
                tenant_id=1,
                account_id=11,
                desired_online=True,
                online_status="online",
                stale_after_at=now_value + timedelta(minutes=5),
            )
        )
        session.add(
            _cycle_action(
                "action-missing-memory",
                now_value,
                {
                    "group_id": 7,
                    "message_text": "旧规划不能绕过消息记忆",
                    "review_approved": True,
                },
            )
        )
        session.commit()
        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *args, **kwargs: object())
        monkeypatch.setattr(
            dispatcher.gateway,
            "send_message",
            lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("missing memory must not call TG")),
        )

        [claimed] = claim_actions(session, limit=1, worker_id="worker-test")

        assert dispatcher.dispatch_action(session, claimed) is True
        action = session.get(Action, "action-missing-memory")
        assert action.status == "failed"
        assert action.result["error_code"] == "ai_message_memory_missing"
        assert action.result["validation_stage"] == "ai_message_memory"


@pytest.mark.no_postgres
def test_group_ai_permission_failure_marks_message_memory_failed(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()

    with Session(engine) as session:
        _add_cycle_skip_basics(session, now_value)
        link = session.scalar(select(TgGroupAccount).where(TgGroupAccount.group_id == 7, TgGroupAccount.account_id == 11))
        link.can_send = False
        _add_group_ai_send_action_with_online_state(
            session,
            now_value,
            online_status="online",
            action_id="action-permission-memory",
            memory_id="memory-permission-send",
            text="权限失败也要回写记忆",
        )
        session.commit()
        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *args, **kwargs: object())
        monkeypatch.setattr(
            dispatcher.gateway,
            "send_message",
            lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("permission failure must not call TG")),
        )

        [claimed] = claim_actions(session, limit=1, worker_id="worker-test")

        assert dispatcher.dispatch_action(session, claimed) is True
        action = session.get(Action, "action-permission-memory")
        assert action.status == "failed"
        assert action.result["validation_stage"] == "account_target_permission"
        memory = session.get(AiGroupMessageMemory, "memory-permission-send")
        assert memory.status == "failed"
        assert memory.action_id == "action-permission-memory"
        assert memory.result["validation_stage"] == "account_target_permission"


@pytest.mark.no_postgres
def test_group_ai_send_rechecks_message_memory_before_gateway(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()

    with Session(engine) as session:
        _add_cycle_skip_basics(session, now_value)
        session.add(
            TgAccountOnlineState(
                tenant_id=1,
                account_id=11,
                desired_online=True,
                online_status="online",
                stale_after_at=now_value + timedelta(minutes=5),
            )
        )
        session.add_all(
            [
                AiGroupMessageMemory(
                    id="memory-current-send",
                    tenant_id=1,
                    group_id=7,
                    task_id="task-cycle-skip",
                    account_id=11,
                    raw_text="花花老师身材服务真好",
                    normalized_text="花花老师身材服务真好",
                    text_fingerprint="current-send",
                    status="reserved",
                    planned_at=now_value,
                ),
                AiGroupMessageMemory(
                    id="memory-conflict-send",
                    tenant_id=1,
                    group_id=7,
                    task_id="other-task",
                    account_id=12,
                    raw_text="花花老师服务身材真好",
                    normalized_text="花花老师服务身材真好",
                    text_fingerprint="conflict-send",
                    status="success",
                    planned_at=now_value + timedelta(seconds=30),
                ),
            ]
        )
        session.add(
            _cycle_action(
                "action-duplicate-send",
                now_value,
                {
                    "group_id": 7,
                    "message_text": "花花老师身材服务真好",
                    "review_approved": True,
                    "slot_id": "task-cycle-skip:cycle:1:turn:1",
                    "ai_message_memory_id": "memory-current-send",
                },
            )
        )
        session.commit()
        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *args, **kwargs: object())
        monkeypatch.setattr(
            dispatcher.gateway,
            "send_message",
            lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("duplicate must not call TG")),
        )

        [claimed] = claim_actions(session, limit=1, worker_id="worker-test")

        assert dispatcher.dispatch_action(session, claimed) is True
        action = session.get(Action, "action-duplicate-send")
        assert action.status == "failed"
        assert action.result["error_code"] == "duplicate_message"
        assert action.result["validation_stage"] == "ai_message_memory"
        memory = session.get(AiGroupMessageMemory, "memory-current-send")
        assert memory.status == "duplicate_before_send"
        assert memory.result["duplicate_reference_id"] == "memory-conflict-send"


@pytest.mark.no_postgres
def test_group_ai_send_success_updates_account_stance_memory(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()

    with Session(engine) as session:
        _add_cycle_skip_basics(session, now_value)
        session.add(
            TgAccountOnlineState(
                tenant_id=1,
                account_id=11,
                desired_online=True,
                online_status="online",
                stale_after_at=now_value + timedelta(minutes=5),
            )
        )
        session.add(
            AiGroupMessageMemory(
                id="memory-stance-send",
                tenant_id=1,
                group_id=7,
                task_id="task-cycle-skip",
                account_id=11,
                raw_text="花花老师这个感觉可以问问",
                normalized_text="花花老师这个感觉可以问问",
                text_fingerprint="stance-send",
                status="reserved",
                planned_at=now_value,
            )
        )
        session.add(
            _cycle_action(
                "action-stance-send",
                now_value,
                {
                    "group_id": 7,
                    "message_text": "花花老师这个感觉可以问问",
                    "review_approved": True,
                    "slot_id": "task-cycle-skip:cycle:1:turn:1",
                    "ai_message_memory_id": "memory-stance-send",
                    "topic_direction": {"title": "郑州楼凤妹子怎么样"},
                    "teacher_target": {"name": "花花老师"},
                    "act_type": "追问",
                    "semantic_cluster": "teacher_price_question",
                },
            )
        )
        session.commit()
        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *args, **kwargs: object())
        monkeypatch.setattr(dispatcher.gateway, "send_message", lambda *args, **kwargs: SendResult(True, remote_message_id="tg-stance-ok"))

        [claimed] = claim_actions(session, limit=1, worker_id="worker-test")

        assert dispatcher.dispatch_action(session, claimed) is True
        stance = session.scalar(select(AiAccountGroupStanceMemory).where(AiAccountGroupStanceMemory.account_id == 11))

    assert stance is not None
    assert stance.group_id == 7
    assert stance.topic_direction == "郑州楼凤妹子怎么样"
    assert stance.teacher_target == "花花老师"
    assert stance.last_act_type == "question"
    assert stance.last_semantic_cluster == "teacher_price_question"
    assert stance.last_message_id == "tg-stance-ok"
    assert "花花老师这个感觉可以问问" in stance.summary


@pytest.mark.no_postgres
def test_hard_hourly_plain_send_ignores_context_expiration(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()

    with Session(engine) as session:
        _add_cycle_skip_basics(session, now_value)
        old_context, _new_context = _add_cycle_contexts(session, now_value)
        gate_payload = _add_cycle_ai_send_gate_state(
            session,
            now_value,
            memory_id="memory-hard-hourly-due",
            text="hard target send",
        )
        payload = {
            **_expired_cycle_payload(old_context.id, text="hard target send"),
            **gate_payload,
            "hard_hourly_target": True,
            "hard_hourly_bucket": now_value.replace(minute=0, second=0, microsecond=0).isoformat(),
        }
        session.add(_cycle_action("action-hard-hourly-due", now_value, payload))
        session.commit()
        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *args, **kwargs: object())
        monkeypatch.setattr(
            dispatcher.gateway,
            "send_message",
            lambda *args, **kwargs: SendResult(True, remote_message_id="tg-hard-hourly"),
        )

        [claimed] = claim_actions(session, limit=1, worker_id="worker-test")

        assert dispatcher.dispatch_action(session, claimed) is True
        action = session.get(Action, "action-hard-hourly-due")
        assert action.status == "success"
        assert action.result["telegram_msg_id"] == "tg-hard-hourly"
        assert action.result.get("error_code") != "context_expired"


@pytest.mark.no_postgres
def test_hard_hourly_reply_send_keeps_context_expiration(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()

    with Session(engine) as session:
        _add_cycle_skip_basics(session, now_value)
        old_context, _new_context = _add_cycle_contexts(session, now_value)
        payload = {
            **_expired_cycle_payload(old_context.id, text="hard target reply"),
            "hard_hourly_target": True,
            "reply_to_message_id": 1001,
        }
        session.add(_cycle_action("action-hard-hourly-reply", now_value, payload))
        session.commit()
        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *args, **kwargs: object())
        monkeypatch.setattr(
            dispatcher.gateway,
            "send_message",
            lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("expired reply must not call TG")),
        )

        [claimed] = claim_actions(session, limit=1, worker_id="worker-test")

        assert dispatcher.dispatch_action(session, claimed) is True
        action = session.get(Action, "action-hard-hourly-reply")
        assert action.status == "skipped"
        assert action.result["error_code"] == "context_expired"


@pytest.mark.no_postgres
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
        gate_payload = _add_cycle_ai_send_gate_state(
            session,
            now_value,
            memory_id="memory-backfill-send",
            text="should send",
        )
        session.add(
            _cycle_action(
                "action-backfill",
                now_value,
                {
                    **_expired_cycle_payload(snapshot.id, cycle_id="cycle-backfill", text="should send"),
                    **gate_payload,
                },
            )
        )
        session.commit()
        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *args, **kwargs: object())
        monkeypatch.setattr(dispatcher.gateway, "send_message", lambda *args, **kwargs: SendResult(True, remote_message_id="tg-context-ok"))

        [claimed] = claim_actions(session, limit=1, worker_id="worker-test")

        assert dispatcher.dispatch_action(session, claimed) is True

        refreshed = session.get(Action, "action-backfill")
        assert refreshed.status == "success"
        assert refreshed.result["telegram_msg_id"] == "tg-context-ok"


@pytest.mark.no_postgres
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


@pytest.mark.no_postgres
def test_group_ai_gateway_unknown_updates_memory_and_stance(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()

    with Session(engine) as session:
        _add_cycle_skip_basics(session, now_value)
        session.add(
            TgAccountOnlineState(
                tenant_id=1,
                account_id=11,
                desired_online=True,
                online_status="online",
                stale_after_at=now_value + timedelta(minutes=5),
            )
        )
        session.add(
            AiGroupMessageMemory(
                id="memory-unknown-send",
                tenant_id=1,
                group_id=7,
                task_id="task-cycle-skip",
                account_id=11,
                raw_text="主任这个可以先问价格",
                normalized_text="主任这个可以先问价格",
                text_fingerprint="unknown-send",
                status="reserved",
                planned_at=now_value,
            )
        )
        session.add(
            _cycle_action(
                "action-unknown-ai-send",
                now_value,
                {
                    "group_id": 7,
                    "message_text": "主任这个可以先问价格",
                    "review_approved": True,
                    "slot_id": "task-cycle-skip:cycle:1:turn:1",
                    "ai_message_memory_id": "memory-unknown-send",
                    "topic_direction": {"title": "精品榜"},
                    "teacher_target": {"name": "主任"},
                    "act_type": "追问",
                },
            )
        )
        session.commit()
        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *args, **kwargs: object())
        monkeypatch.setattr(dispatcher.gateway, "send_message", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("socket lost after send")))

        [claimed] = claim_actions(session, limit=1, worker_id="worker-test")

        assert dispatcher.dispatch_action(session, claimed) is True
        action = session.get(Action, "action-unknown-ai-send")
        memory = session.get(AiGroupMessageMemory, "memory-unknown-send")
        stance = session.scalar(select(AiAccountGroupStanceMemory).where(AiAccountGroupStanceMemory.account_id == 11))

    assert action.status == "unknown_after_send"
    assert memory.status == "unknown_after_send"
    assert memory.action_id == "action-unknown-ai-send"
    assert stance is not None
    assert stance.teacher_target == "主任"
    assert stance.last_message_id == "action-unknown-ai-send"
    assert "主任这个可以先问价格" in stance.summary


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
        gate_payload = _add_group_ai_send_gate_payload(
            session,
            now_value,
            action_id="action-permission",
            task_id="task-permission",
            group_id=7,
            account_id=11,
            text="hello",
        )
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
                payload={"group_id": 7, "message_text": "hello", "review_approved": True, **gate_payload},
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


def test_group_ai_chat_permission_denied_over_threshold_creates_rescue_action(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()

    with Session(engine) as session:
        tenant = Tenant(id=1, name="默认运营空间")
        tenant.group_rescue_enabled = True
        tenant.group_rescue_admin_account_id = 99
        session.add(tenant)
        session.add(Task(id="task-ai-rescue", tenant_id=1, name="ai rescue", type="group_ai_chat", status="running"))
        session.add(TgAccount(id=11, tenant_id=1, display_name="普通账号", username="normal_user", phone_masked="+861***0011", status="在线", session_ciphertext="session-11"))
        session.add(TgAccount(id=99, tenant_id=1, display_name="救援账号", phone_masked="+861***0099", status="在线", session_ciphertext="session-99"))
        session.add(OperationTarget(id=21, tenant_id=1, target_type="group", tg_peer_id="-1007", title="运营群", auth_status="已授权运营", can_send=True))
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="运营群", auth_status="已授权运营", can_send=True, require_review=False))
        session.add(TgGroupAccount(tenant_id=1, group_id=7, account_id=11, can_send=True, permission_label="已加入"))
        for index in range(3):
            session.add(
                Action(
                    id=f"previous-denied-{index}",
                    tenant_id=1,
                    task_id="task-ai-rescue",
                    task_type="group_ai_chat",
                    action_type="send_message",
                    account_id=11,
                    status="failed",
                    scheduled_at=now_value - timedelta(minutes=index + 1),
                    payload={"group_id": 7, "message_text": "old", "review_approved": True},
                    result={"success": False, "error_code": FailureType.GROUP_PERMISSION_DENIED.value, "error_message": "群黑名单，无法发言"},
                )
            )
        gate_payload = _add_group_ai_send_gate_payload(
            session,
            now_value,
            action_id="current-denied",
            task_id="task-ai-rescue",
            group_id=7,
            account_id=11,
            text="hello",
        )
        session.add(
            Action(
                id="current-denied",
                tenant_id=1,
                task_id="task-ai-rescue",
                task_type="group_ai_chat",
                action_type="send_message",
                account_id=11,
                status="pending",
                scheduled_at=now_value,
                payload={"group_id": 7, "operation_target_id": 21, "message_text": "hello", "review_approved": True, **gate_payload},
            )
        )
        session.commit()

        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *args, **kwargs: object())
        monkeypatch.setattr(
            dispatcher.gateway,
            "send_message",
            lambda *args, **kwargs: SendResult(False, failure_type=FailureType.GROUP_PERMISSION_DENIED.value, detail="群黑名单，无法发言"),
        )

        [claimed] = claim_actions(session, limit=1, worker_id="worker-test")
        assert dispatcher.dispatch_action(session, claimed) is True

        rescue_actions = session.scalars(select(Action).where(Action.action_type == "invite_group_account")).all()
        assert len(rescue_actions) == 1
        assert rescue_actions[0].account_id == 99
        assert rescue_actions[0].payload["trigger_account_id"] == 11
        assert rescue_actions[0].payload["target_account_ref"] == "@normal_user"
        assert rescue_actions[0].payload["trigger_reason"] == "群黑名单，无法发言"


def test_target_membership_requires_send_rechecks_existing_group_link(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(Task(id="task-membership", tenant_id=1, name="membership", type="group_ai_chat", status="running"))
        session.add(
            OperationTarget(
                id=21,
                tenant_id=1,
                target_type="group",
                tg_peer_id="-10021",
                title="目标群",
                auth_status="已授权运营",
                can_send=True,
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
                tg_peer_id="-10021",
                title="目标群",
                auth_status="已授权运营",
                can_send=True,
                require_review=False,
            )
        )
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

        joined: list[str] = []
        probe_results = [
            OperationResult(False, "失败", "群无权限", "群无权限或账号不可发言"),
            OperationResult(True, detail="重新入群后可发言"),
        ]
        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *args, **kwargs: object())
        monkeypatch.setattr(
            dispatcher.gateway,
            "ensure_channel_membership",
            lambda _account_id, channel_ref, *_args, **_kwargs: joined.append(channel_ref) or OperationResult(True, detail="重新入群成功"),
        )
        monkeypatch.setattr(
            dispatcher.gateway,
            "probe_target_capabilities",
            lambda *args, **kwargs: probe_results.pop(0),
        )

        action = session.get(Action, "action-membership")
        assert dispatcher.dispatch_action(session, action) is True

        link = session.scalar(select(TgGroupAccount).where(TgGroupAccount.group_id == 7, TgGroupAccount.account_id == 11))
        verification = session.scalar(select(VerificationTask).where(VerificationTask.group_id == 7, VerificationTask.account_id == 11))
        assert joined == ["-10021"]
        assert link is not None
        assert link.can_send is True
        assert action.status == "success"
        assert action.result["membership_status"] == "already_joined"
        assert verification is None


def test_pending_ai_generation_batch_is_scoped_to_generation_cycle():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        batch = pending_generation_cycle_batch(session, _now())
        assert [row.id for row, _payload in batch] == ["action-current", "action-new-sibling"]


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


def _patch_required_channel_send_failure(monkeypatch, followed: list[str], probes: list[str]) -> None:
    monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        dispatcher.gateway,
        "send_message",
        lambda *_args, **_kwargs: SendResult(
            False,
            failure_type=FailureType.GROUP_PERMISSION_DENIED.value,
            detail="学院助手：您需要关注我们的频道才能发言。 [按钮：天津音乐学院车库备用 (https://t.me/qiyue201)]",
        ),
    )
    monkeypatch.setattr(
        dispatcher.gateway,
        "ensure_channel_membership",
        lambda _account_id, channel_ref, *_args, **_kwargs: followed.append(channel_ref) or OperationResult(True, "已处理", detail="已关注"),
    )
    monkeypatch.setattr(
        dispatcher.gateway,
        "probe_target_capabilities",
        lambda _account_id, target_peer_id, *_args, **_kwargs: probes.append(target_peer_id) or OperationResult(True, detail="复检可发言"),
    )


def _seed_required_channel_send_action(session: Session, scheduled_at) -> None:
    session.add(Tenant(id=1, name="默认运营空间"))
    session.add(Task(id="task-send-follow", tenant_id=1, name="send-follow", type="group_ai_chat", status="running"))
    session.add(OperationTarget(id=21, tenant_id=1, target_type="group", tg_peer_id="-10021", title="目标群", auth_status="已授权运营", can_send=True))
    session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-10021", title="目标群", auth_status="已授权运营", can_send=True))
    session.add(TgAccount(id=11, tenant_id=1, display_name="账号", phone_masked="+861***0011", status="在线", session_ciphertext="session"))
    session.add(TgGroupAccount(tenant_id=1, group_id=7, account_id=11, can_send=True, permission_label="可发言"))
    gate_payload = _add_group_ai_send_gate_payload(
        session,
        scheduled_at,
        action_id="action-send-follow",
        task_id="task-send-follow",
        group_id=7,
        account_id=11,
        text="我也关注下这个",
    )
    session.add(
        Action(
            id="action-send-follow",
            tenant_id=1,
            task_id="task-send-follow",
            task_type="group_ai_chat",
            action_type="send_message",
            account_id=11,
            status="pending",
            scheduled_at=scheduled_at,
            payload={"group_id": 7, "operation_target_id": 21, "target_display": "目标群", "message_text": "我也关注下这个", "review_approved": True, **gate_payload},
        )
    )


def test_group_send_failure_follows_required_channel_and_requeues(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    followed: list[str] = []
    probes: list[str] = []
    _patch_required_channel_send_failure(monkeypatch, followed, probes)

    with Session(engine) as session:
        _seed_required_channel_send_action(session, _now())
        session.commit()

        action = session.get(Action, "action-send-follow")
        assert dispatcher.dispatch_action(session, action) is True

        link = session.scalar(select(TgGroupAccount).where(TgGroupAccount.group_id == 7, TgGroupAccount.account_id == 11))
        assert followed == ["qiyue201"]
        assert probes == ["-10021"]
        assert link is not None and link.can_send is True
        assert action.status == "pending"
        assert action.result["error_code"] == "required_channel_followed_retry"
        assert action.result["required_channels_followed"] == ["qiyue201"]


def test_group_send_permission_denied_auto_verifies_and_requeues(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    probes: list[str] = []
    resolved: list[str] = []

    monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        dispatcher.gateway,
        "send_message",
        lambda *_args, **_kwargs: SendResult(
            False,
            failure_type=FailureType.GROUP_PERMISSION_DENIED.value,
            detail="群无权限或账号不可发言：需要点击按钮完成验证",
        ),
    )
    monkeypatch.setattr(
        dispatcher.gateway,
        "resolve_verification_task",
        lambda _account_id, action, *_args, **_kwargs: resolved.append(action) or OperationResult(True, "已处理", detail="已点击验证按钮"),
    )
    monkeypatch.setattr(
        dispatcher.gateway,
        "probe_target_capabilities",
        lambda _account_id, target_peer_id, *_args, **_kwargs: probes.append(target_peer_id) or OperationResult(True, detail="复检可发言"),
    )

    with Session(engine) as session:
        _seed_required_channel_send_action(session, _now())
        session.commit()

        action = session.get(Action, "action-send-follow")
        assert dispatcher.dispatch_action(session, action) is True

        link = session.scalar(select(TgGroupAccount).where(TgGroupAccount.group_id == 7, TgGroupAccount.account_id == 11))
        verification = session.scalar(select(VerificationTask).where(VerificationTask.group_id == 7, VerificationTask.account_id == 11))
        assert action.status == "pending"
        assert action.result["error_code"] == "send_permission_recovered_retry"
        assert action.result["verification_task_id"] == verification.id
        assert link is not None and link.can_send is True
        assert verification is not None and verification.status == "已处理"
        assert resolved == ["点击按钮"]
        assert probes == ["-10021"]


def test_target_membership_follows_linked_channel_when_join_entry_is_blocked(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(Task(id="task-linked-entry", tenant_id=1, name="linked-entry", type="group_ai_chat", status="running"))
        session.add(OperationTarget(id=21, tenant_id=1, target_type="group", tg_peer_id="-10021", title="目标群", auth_status="已授权运营", can_send=True))
        session.add(TgAccount(id=11, tenant_id=1, display_name="账号", phone_masked="+861***0011", status="在线", session_ciphertext="session"))
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-10021", title="目标群", auth_status="已授权运营", can_send=True, require_review=False))
        session.add(
            Action(
                id="action-linked-entry",
                tenant_id=1,
                task_id="task-linked-entry",
                task_type="group_ai_chat",
                action_type="ensure_target_membership",
                account_id=11,
                status="pending",
                scheduled_at=_now(),
                payload={"channel_id": "-10021", "channel_target_id": 21, "target_type": "group", "target_display": "目标群", "require_send": True},
            )
        )
        session.commit()

        followed: list[tuple[int, str]] = []
        joined: list[str] = []

        def fake_ensure_membership(_account_id, target_peer_id, *_args, **_kwargs):
            joined.append(target_peer_id)
            if len(joined) == 1:
                return OperationResult(False, "失败", "群无权限", "缓存频道不可访问 / 账号无权限")
            return OperationResult(True, "已处理", detail="已加入目标群")

        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *args, **kwargs: object())
        monkeypatch.setattr(dispatcher.gateway, "ensure_channel_membership", fake_ensure_membership)
        monkeypatch.setattr(dispatcher.gateway, "probe_target_capabilities", lambda *args, **kwargs: OperationResult(True, detail="group:-10021:可访问"))
        monkeypatch.setattr(
            dispatcher.gateway,
            "ensure_linked_channel_membership",
            lambda account_id, target_peer_id, *args, **kwargs: followed.append((account_id, target_peer_id)) or OperationResult(True, "已处理", detail="已关注关联频道"),
            raising=False,
        )

        action = session.get(Action, "action-linked-entry")
        assert dispatcher.dispatch_action(session, action) is True

        link = session.scalar(select(TgGroupAccount).where(TgGroupAccount.group_id == 7, TgGroupAccount.account_id == 11))
        assert joined == ["-10021", "-10021"]
        assert followed == [(11, "-10021")]
        assert link is not None and link.can_send is True
        assert action.status == "success"
        assert action.result["membership_status"] == "joined"
        assert action.result["prerequisite_channel_followed"] is True
        assert action.result["target_membership_retried_after_required_channel"] is True
        assert session.scalar(select(VerificationTask).where(VerificationTask.group_id == 7)) is None


def test_target_membership_classifies_frozen_account_as_unavailable(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(Task(id="task-frozen-entry", tenant_id=1, name="frozen-entry", type="group_ai_chat", status="running"))
        session.add(OperationTarget(id=21, tenant_id=1, target_type="group", tg_peer_id="-10021", title="目标群", auth_status="已授权运营", can_send=True))
        session.add(TgAccount(id=11, tenant_id=1, display_name="账号", phone_masked="+861***0011", status="在线", session_ciphertext="session"))
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-10021", title="目标群", auth_status="已授权运营", can_send=True, require_review=False))
        session.add(
            Action(
                id="action-frozen-entry",
                tenant_id=1,
                task_id="task-frozen-entry",
                task_type="group_ai_chat",
                action_type="ensure_target_membership",
                account_id=11,
                status="pending",
                scheduled_at=now_value,
                payload={
                    "channel_id": "-10021",
                    "channel_target_id": 21,
                    "target_type": "group",
                    "target_display": "目标群",
                    "require_send": True,
                },
            )
        )
        session.commit()

        frozen_detail = "You tried to use a method that is not available for frozen accounts (caused by JoinChannelRequest)"
        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *args, **kwargs: object())
        monkeypatch.setattr(
            dispatcher.gateway,
            "ensure_channel_membership",
            lambda *args, **kwargs: OperationResult(False, "失败", "未知错误", frozen_detail),
        )

        action = session.get(Action, "action-frozen-entry")
        assert dispatcher.dispatch_action(session, action) is True

        assert action.status == "failed"
        assert action.result["error_code"] == "账号不可用"
        assert action.result["validation_stage"] == "telegram_api"
        attempt = session.scalar(select(ExecutionAttempt).where(ExecutionAttempt.action_id == action.id))
        assert attempt is not None
        assert attempt.failure_type == "账号不可用"
        assert frozen_detail in attempt.failure_detail
        account = session.get(TgAccount, 11)
        assert account.status == "疑似封禁"
        assert account.health_score <= 20


def test_hard_hourly_membership_claim_bypasses_send_capacity_policy():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(SchedulingSetting(tenant_id=1, default_account_hour_limit=1))
        session.add(TgAccount(id=11, tenant_id=1, display_name="准入号", phone_masked="+861***0011", status="在线"))
        session.add(
            Task(
                id="task-hard-membership-policy",
                tenant_id=1,
                name="硬目标准入",
                type="group_ai_chat",
                status="running",
                type_config={
                    "hard_hourly_target_enabled": True,
                    "hourly_min_messages": 300,
                },
            )
        )
        session.add(
            Action(
                id="action-prior-send",
                tenant_id=1,
                task_id="task-hard-membership-policy",
                task_type="group_ai_chat",
                action_type="send_message",
                account_id=11,
                status="success",
                scheduled_at=now_value,
                executed_at=now_value,
                payload={"message_text": "上一条"},
            )
        )
        session.add(
            Action(
                id="action-hard-membership",
                tenant_id=1,
                task_id="task-hard-membership-policy",
                task_type="group_ai_chat",
                action_type="ensure_target_membership",
                account_id=11,
                status="pending",
                scheduled_at=now_value,
                payload={
                    "channel_id": "-10021",
                    "channel_target_id": 21,
                    "target_type": "group",
                    "target_display": "目标群",
                    "require_send": True,
                },
                result={},
            )
        )
        session.commit()

        action = session.get(Action, "action-hard-membership")
        assert dispatcher._apply_claim_account_policy(session, action) is True

        assert action.status == "pending"
        assert action.scheduled_at == now_value
        assert action.result == {}


def test_hard_hourly_send_claim_bypasses_send_capacity_policy(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()
    monkeypatch.setattr(dispatcher, "get_settings", lambda: _redis_bucket_settings(enable_redis_token_bucket=False))

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(SchedulingSetting(tenant_id=1, default_account_hour_limit=1, jitter_min_seconds=0, jitter_max_seconds=0))
        session.add(TgAccount(id=11, tenant_id=1, display_name="硬目标号", phone_masked="+861***0011", status="在线"))
        session.add(
            Task(
                id="task-hard-send-policy", tenant_id=1, name="硬目标发言", type="group_ai_chat", status="running",
                priority=9,
                type_config={"hard_hourly_target_enabled": True, "hourly_min_messages": 300},
            )
        )
        session.add_all(
            [
                Action(
                    id="action-prior-send",
                    tenant_id=1, task_id="task-hard-send-policy", task_type="group_ai_chat", action_type="send_message",
                    account_id=11,
                    status="success",
                    scheduled_at=now_value,
                    executed_at=now_value,
                    payload={"message_text": "上一条"},
                ),
                Action(
                    id="action-hard-send",
                    tenant_id=1, task_id="task-hard-send-policy", task_type="group_ai_chat", action_type="send_message",
                    account_id=11,
                    status="pending",
                    scheduled_at=now_value,
                    payload={"message_text": "硬目标补量", "hard_hourly_target": True},
                ),
            ]
        )
        session.commit()

        claimed = claim_actions(session, limit=1, worker_id="worker-hard-hourly")

        action = session.get(Action, "action-hard-send")
        assert [item.id for item in claimed] == ["action-hard-send"]
        assert action.status == "executing"
        assert action.result["account_policy_action"] == "hard_hourly_capacity_override"


def test_hard_hourly_plan_slot_ignores_send_capacity_policy():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(SchedulingSetting(tenant_id=1, default_account_hour_limit=1, jitter_min_seconds=0, jitter_max_seconds=0))
        account = TgAccount(id=11, tenant_id=1, display_name="硬目标号", phone_masked="+861***0011", status="在线")
        task = Task(
            id="task-hard-plan-policy",
            tenant_id=1,
            name="硬目标规划",
            type="group_ai_chat",
            status="running",
            priority=9,
            type_config={"hard_hourly_target_enabled": True, "hourly_min_messages": 300},
        )
        session.add_all([account, task])
        session.add(
            Action(
                id="action-prior-plan-send",
                tenant_id=1,
                task_id=task.id,
                task_type="group_ai_chat",
                action_type="send_message",
                account_id=account.id,
                status="success",
                scheduled_at=now_value,
                executed_at=now_value,
                payload={"message_text": "上一条"},
            )
        )
        session.commit()

        chosen, planned_at = group_ai_chat._choose_capacity_slot(
            session,
            task,
            [account],
            now_value,
            0,
            set(),
            True,
            {"goal": 300, "deficit": 300, "bucket": now_value.isoformat()},
            [],
            group_ai_chat.AccountCapacityCache(),
        )

        assert chosen.id == account.id
        assert planned_at == now_value


def test_target_membership_skips_when_joined_probe_still_cannot_send(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(Task(id="task-probe-denied", tenant_id=1, name="probe-denied", type="group_ai_chat", status="running"))
        session.add(OperationTarget(id=21, tenant_id=1, target_type="group", tg_peer_id="-10021", title="目标群", auth_status="已授权运营", can_send=True))
        session.add(TgAccount(id=11, tenant_id=1, display_name="账号", phone_masked="+861***0011", status="在线", session_ciphertext="session"))
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-10021", title="目标群", auth_status="已授权运营", can_send=True, require_review=False))
        session.add(
            Action(
                id="action-probe-denied",
                tenant_id=1,
                task_id="task-probe-denied",
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
        monkeypatch.setattr(dispatcher.gateway, "ensure_channel_membership", lambda *args, **kwargs: OperationResult(True, detail="joined"))
        monkeypatch.setattr(dispatcher.gateway, "probe_target_capabilities", lambda *args, **kwargs: OperationResult(False, "失败", "群无权限", "缓存频道不可访问 / 账号无权限"))
        monkeypatch.setattr(dispatcher.gateway, "ensure_linked_channel_membership", lambda *args, **kwargs: OperationResult(True, "已处理", detail="已关注关联频道"), raising=False)

        action = session.get(Action, "action-probe-denied")
        assert dispatcher.dispatch_action(session, action) is True

        assert action.status == "skipped"
        assert action.result["error_code"] == "membership_permission_denied"
        assert action.result["membership_status"] == "permission_denied"
        verification = session.scalar(select(VerificationTask).where(VerificationTask.group_id == 7, VerificationTask.account_id == 11))
        assert verification is not None
        assert verification.suggested_action == "关注频道"
        assert action.result["verification_task_id"] == verification.id


def test_target_membership_retries_join_after_required_channel_follow(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(Task(id="task-follow-then-join", tenant_id=1, name="follow-then-join", type="group_ai_chat", status="running"))
        session.add(OperationTarget(id=21, tenant_id=1, target_type="group", tg_peer_id="-10021", title="天津音乐学院", auth_status="已授权运营", can_send=True))
        session.add(TgAccount(id=11, tenant_id=1, display_name="账号", phone_masked="+861***0011", status="在线", session_ciphertext="session"))
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-10021", title="天津音乐学院", auth_status="已授权运营", can_send=True))
        session.add(
            Action(
                id="action-follow-then-join",
                tenant_id=1,
                task_id="task-follow-then-join",
                task_type="group_ai_chat",
                action_type="ensure_target_membership",
                account_id=11,
                status="pending",
                scheduled_at=now_value,
                payload={"channel_id": "-10021", "channel_target_id": 21, "target_type": "group", "target_display": "天津音乐学院", "require_send": True},
            )
        )
        session.commit()

        joins: list[str] = []

        def ensure_membership(_account_id, channel_ref, *_args, **_kwargs):
            joins.append(channel_ref)
            if channel_ref == "-10021" and joins.count("-10021") == 1:
                return OperationResult(False, "失败", FailureType.GROUP_PERMISSION_DENIED.value, "您需要关注我们的频道才能发言。 [按钮：天津音乐学院车库备用 (https://t.me/qiyue201)]")
            return OperationResult(True, detail="joined")

        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *args, **kwargs: object())
        monkeypatch.setattr(dispatcher.gateway, "ensure_channel_membership", ensure_membership)
        monkeypatch.setattr(dispatcher.gateway, "probe_target_capabilities", lambda *args, **kwargs: OperationResult(True, detail="可发言"))

        action = session.get(Action, "action-follow-then-join")
        assert dispatcher.dispatch_action(session, action) is True

        assert joins == ["-10021", "qiyue201", "-10021"]
        assert action.status == "success"
        assert action.result["target_membership_retried_after_required_channel"] is True
        assert action.result["required_channels_followed"] == ["qiyue201"]


def test_group_send_permission_denied_classifies_button_verification(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="目标群", auth_status="已授权运营", can_send=True))
        session.add(OperationTarget(id=21, tenant_id=1, target_type="group", tg_peer_id="-1007", title="目标群", auth_status="已授权运营", can_send=True))
        session.add(TgAccount(id=11, tenant_id=1, display_name="账号11", phone_masked="+861***0011", status="在线", session_ciphertext="cipher"))
        session.add(TgGroupAccount(tenant_id=1, group_id=7, account_id=11, can_send=True))
        session.add(
            Task(
                id="task-button-verification",
                tenant_id=1,
                name="按钮验证",
                type="group_ai_chat",
                status="running",
                account_config={"selection_mode": "all"},
                pacing_config={},
                type_config={},
            )
        )
        session.add(
            Action(
                id="action-button-verification",
                tenant_id=1,
                task_id="task-button-verification",
                task_type="group_ai_chat",
                action_type="ensure_target_membership",
                account_id=11,
                scheduled_at=now_value,
                payload={"channel_id": "-1007", "channel_target_id": 21, "target_type": "group", "target_display": "目标群", "require_send": True},
            )
        )
        session.commit()

        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *args, **kwargs: object())
        monkeypatch.setattr(dispatcher.gateway, "ensure_channel_membership", lambda *args, **kwargs: OperationResult(True, detail="joined"))
        monkeypatch.setattr(dispatcher.gateway, "probe_target_capabilities", lambda *args, **kwargs: OperationResult(False, "失败", "群验证", "需要点击按钮完成验证"))

        action = session.get(Action, "action-button-verification")
        assert dispatcher.dispatch_action(session, action) is True

        verification = session.scalar(select(VerificationTask).where(VerificationTask.group_id == 7, VerificationTask.account_id == 11))
        assert verification is not None
        assert verification.suggested_action == "点击按钮"


def test_target_membership_image_verification_uses_reader_candidates(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()
    probe_calls = {"count": 0}
    captured_reader_ids: list[int] = []

    def fake_probe(*_args, **_kwargs):
        probe_calls["count"] += 1
        if probe_calls["count"] == 1:
            return OperationResult(False, "失败", "群无权限", "未解析到群关联频道")
        return OperationResult(True, "已完成", detail="验证码后可发言")

    def fake_auto_resolve(_session, _task, _account, _credentials, *, reader_candidates=None):
        captured_reader_ids.extend(account.id for account, _cred in reader_candidates or [])
        return OperationResult(True, "已处理", detail="MiMo 已识别并提交验证码")

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="目标群", auth_status="已授权运营", can_send=True))
        session.add(OperationTarget(id=21, tenant_id=1, target_type="group", tg_peer_id="-1007", title="目标群", auth_status="已授权运营", can_send=True))
        session.add_all(
            [
                TgAccount(id=11, tenant_id=1, display_name="加入账号", phone_masked="+861***0011", status="在线", session_ciphertext="cipher-11"),
                TgAccount(id=12, tenant_id=1, display_name="可读账号", phone_masked="+861***0012", status="在线", session_ciphertext="cipher-12"),
            ]
        )
        session.add(TgGroupAccount(tenant_id=1, group_id=7, account_id=12, can_send=True))
        session.add(Task(id="task-image-reader", tenant_id=1, name="图形验证", type="target_admission_retry", status="running"))
        session.add(
            Action(
                id="action-image-reader",
                tenant_id=1,
                task_id="task-image-reader",
                task_type="target_admission_retry",
                action_type="ensure_target_membership",
                account_id=11,
                scheduled_at=now_value,
                payload={"channel_id": "-1007", "channel_target_id": 21, "target_type": "group", "target_display": "目标群", "require_send": True},
            )
        )
        session.commit()

        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *_args, **_kwargs: object())
        monkeypatch.setattr(dispatcher.gateway, "ensure_channel_membership", lambda *_args, **_kwargs: OperationResult(True, detail="joined"))
        monkeypatch.setattr(dispatcher.gateway, "probe_target_capabilities", fake_probe)
        monkeypatch.setattr(dispatcher, "auto_resolve_image_verification", fake_auto_resolve)

        action = session.get(Action, "action-image-reader")
        assert dispatcher.dispatch_action(session, action) is True

        verification = session.scalar(select(VerificationTask).where(VerificationTask.group_id == 7, VerificationTask.account_id == 11))
        assert verification is not None
        assert verification.suggested_action == "识别图形验证码"
        assert captured_reader_ids == [12]
        assert action.status == "success"
        assert action.result["membership_status"] == "joined"


def test_target_membership_auto_verification_rechecks_send_permission(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()
    probe_calls = {"count": 0}
    resolve_calls: list[str] = []

    def fake_probe(*_args, **_kwargs):
        probe_calls["count"] += 1
        if probe_calls["count"] == 1:
            return OperationResult(False, "失败", "群验证", "需要点击按钮完成验证")
        return OperationResult(True, "已完成", detail="验证后可发言")

    def fake_resolve(_account_id, action, *_args, **_kwargs):
        resolve_calls.append(action)
        return OperationResult(True, "已处理", detail="已点击首个验证按钮")

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="目标群", auth_status="已授权运营", can_send=True))
        session.add(OperationTarget(id=21, tenant_id=1, target_type="group", tg_peer_id="-1007", title="目标群", auth_status="已授权运营", can_send=True))
        session.add(TgAccount(id=11, tenant_id=1, display_name="账号11", phone_masked="+861***0011", status="在线", session_ciphertext="cipher"))
        session.add(TgGroupAccount(tenant_id=1, group_id=7, account_id=11, can_send=True))
        session.add(Task(id="task-auto-verification", tenant_id=1, name="自动验证", type="group_ai_chat", status="running"))
        session.add(
            Action(
                id="action-auto-verification",
                tenant_id=1,
                task_id="task-auto-verification",
                task_type="group_ai_chat",
                action_type="ensure_target_membership",
                account_id=11,
                scheduled_at=now_value,
                payload={"channel_id": "-1007", "channel_target_id": 21, "target_type": "group", "target_display": "目标群", "require_send": True},
            )
        )
        session.commit()

        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *args, **kwargs: object())
        monkeypatch.setattr(dispatcher.gateway, "ensure_channel_membership", lambda *args, **kwargs: OperationResult(True, detail="joined"))
        monkeypatch.setattr(dispatcher.gateway, "probe_target_capabilities", fake_probe)
        monkeypatch.setattr(dispatcher.gateway, "resolve_verification_task", fake_resolve)

        action = session.get(Action, "action-auto-verification")
        assert dispatcher.dispatch_action(session, action) is True

        link = session.scalar(select(TgGroupAccount).where(TgGroupAccount.group_id == 7, TgGroupAccount.account_id == 11))
        verification = session.scalar(select(VerificationTask).where(VerificationTask.group_id == 7, VerificationTask.account_id == 11))
        assert action.status == "success"
        assert action.result["membership_status"] == "joined"
        assert link is not None and link.can_send is True
        assert verification is not None and verification.status == "已处理"
        assert resolve_calls == ["点击按钮"]
        assert probe_calls["count"] == 2


def test_target_membership_claim_does_not_reassign_account_on_cooldown(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(Task(id="task-membership-cooldown", tenant_id=1, name="membership", type="group_ai_chat", status="running"))
        session.add_all(
            [
                TgAccount(id=11, tenant_id=1, display_name="原账号", phone_masked="+861***0011", status="在线", session_ciphertext="session-11"),
                TgAccount(id=12, tenant_id=1, display_name="替换账号", phone_masked="+861***0012", status="在线", session_ciphertext="session-12"),
            ]
        )
        session.add(
            Action(
                id="action-membership-cooldown",
                tenant_id=1,
                task_id="task-membership-cooldown",
                task_type="group_ai_chat",
                action_type="ensure_target_membership",
                account_id=11,
                status="pending",
                scheduled_at=now_value,
                payload={"channel_id": "-10021", "channel_target_id": 21, "target_type": "group", "target_display": "目标群", "require_send": True},
            )
        )
        session.commit()

        monkeypatch.setattr(
            dispatcher,
            "account_capacity_decision",
            lambda *args, **kwargs: SimpleNamespace(available=False, defer_until=now_value + timedelta(minutes=3), reason="账号冷却中"),
        )
        monkeypatch.setattr(dispatcher, "_replacement_account_for_action", lambda *args, **kwargs: session.get(TgAccount, 12))

        assert claim_actions(session, limit=1, worker_id="worker-test") == []

        action = session.get(Action, "action-membership-cooldown")
        assert action.account_id == 11
        assert action.status == "pending"
        assert action.result["validation_stage"] == "account_policy"
        assert action.result.get("account_policy_action") != "reassigned"


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


@pytest.mark.no_postgres
def test_claim_actions_prioritizes_due_hard_hourly_send_actions(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()
    monkeypatch.setattr(dispatcher, "get_settings", lambda: _redis_bucket_settings(enable_redis_token_bucket=False))

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add_all(
            [
                TgAccount(id=11, tenant_id=1, display_name="账号A", phone_masked="+861***0011", status="在线"),
                TgAccount(id=12, tenant_id=1, display_name="账号B", phone_masked="+861***0012", status="在线"),
            ]
        )
        session.add(Task(id="task-comment", tenant_id=1, name="频道评论", type="channel_comment", status="running", priority=1))
        session.add(Task(id="task-hard", tenant_id=1, name="硬目标", type="group_ai_chat", status="running", priority=9))
        session.add_all(
            [
                Action(id="action-comment", tenant_id=1, task_id="task-comment", task_type="channel_comment", action_type="post_comment", account_id=11, status="pending", scheduled_at=now_value - timedelta(minutes=10), payload={"channel_id": "-1001", "message_id": 1, "comment_text": "收到"}),
                Action(id="action-hard", tenant_id=1, task_id="task-hard", task_type="group_ai_chat", action_type="send_message", account_id=12, status="pending", scheduled_at=now_value, payload={"message_text": "hard", "hard_hourly_target": True}),
            ]
        )
        session.commit()

        claimed = claim_actions(session, limit=1, worker_id="worker-hard-hourly")

        assert [action.id for action in claimed] == ["action-hard"]
        assert session.get(Action, "action-comment").status == "pending"


@pytest.mark.no_postgres
def test_claim_actions_prioritizes_due_comment_before_ordinary_batch_action(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()
    monkeypatch.setattr(dispatcher, "get_settings", lambda: _redis_bucket_settings(enable_redis_token_bucket=False))

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add_all(
            [
                TgAccount(id=11, tenant_id=1, display_name="点赞账号", phone_masked="+861***0011", status="在线"),
                TgAccount(id=12, tenant_id=1, display_name="评论账号", phone_masked="+861***0012", status="在线"),
            ]
        )
        session.add_all(
            [
                Task(id="task-like", tenant_id=1, name="批量点赞", type="channel_like", status="running", priority=3),
                Task(id="task-comment", tenant_id=1, name="频道评论", type="channel_comment", status="running", priority=3),
            ]
        )
        session.add_all(
            [
                Action(id="action-like", tenant_id=1, task_id="task-like", task_type="channel_like", action_type="react_message", account_id=11, status="pending", scheduled_at=now_value - timedelta(minutes=10), payload={"channel_id": "-1001", "message_id": 1, "reaction": "👍"}),
                Action(id="action-comment", tenant_id=1, task_id="task-comment", task_type="channel_comment", action_type="post_comment", account_id=12, status="pending", scheduled_at=now_value - timedelta(minutes=1), payload={"channel_id": "-1001", "message_id": 1, "comment_text": "收到"}),
            ]
        )
        session.commit()

        claimed = claim_actions(session, limit=1, worker_id="worker-comment")

        assert [action.id for action in claimed] == ["action-comment"]
        assert session.get(Action, "action-like").status == "pending"


@pytest.mark.no_postgres
def test_claim_actions_prioritizes_due_search_membership_before_ordinary_batch_action(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()
    monkeypatch.setattr(dispatcher, "get_settings", lambda: _redis_bucket_settings(enable_redis_token_bucket=False))

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add_all(
            [
                TgAccount(id=11, tenant_id=1, display_name="批量账号", phone_masked="+861***0011", status="在线"),
                TgAccount(id=12, tenant_id=1, display_name="准入账号", phone_masked="+861***0012", status="在线"),
            ]
        )
        session.add_all(
            [
                Task(id="task-ai", tenant_id=1, name="普通活跃群", type="group_ai_chat", status="running", priority=3),
                Task(id="task-search", tenant_id=1, name="搜索准入", type="search_join_group", status="running", priority=3),
            ]
        )
        session.add_all(
            [
                Action(id="action-batch", tenant_id=1, task_id="task-ai", task_type="group_ai_chat", action_type="send_message", account_id=11, status="pending", scheduled_at=now_value - timedelta(minutes=10), payload={"message_text": "普通批量动作"}),
                Action(id="action-membership", tenant_id=1, task_id="task-search", task_type="search_join_group", action_type="search_join_membership", account_id=12, status="pending", scheduled_at=now_value, payload={}),
            ]
        )
        session.commit()

        claimed = claim_actions(session, limit=1, worker_id="worker-search-membership")

        assert [action.id for action in claimed] == ["action-membership"]
        assert session.get(Action, "action-batch").status == "pending"


@pytest.mark.no_postgres
def test_claim_actions_prioritizes_strict_search_source_before_ordinary_batch_action(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()
    monkeypatch.setattr(dispatcher, "get_settings", lambda: _redis_bucket_settings(enable_redis_token_bucket=False))

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add_all(
            [
                TgAccount(id=11, tenant_id=1, display_name="批量账号", phone_masked="+861***0011", status="在线"),
                TgAccount(id=12, tenant_id=1, display_name="搜索账号", phone_masked="+861***0012", status="在线"),
            ]
        )
        session.add_all(
            [
                Task(id="task-ai", tenant_id=1, name="普通活跃群", type="group_ai_chat", status="running", priority=3),
                Task(
                    id="task-search",
                    tenant_id=1,
                    name="严格搜索点击",
                    type="search_join_group",
                    status="running",
                    priority=3,
                    type_config={"strict_daily_target": True, "daily_click_target_count": 500},
                ),
            ]
        )
        session.add_all(
            [
                Action(id="action-batch", tenant_id=1, task_id="task-ai", task_type="group_ai_chat", action_type="send_message", account_id=11, status="pending", scheduled_at=now_value - timedelta(minutes=10), payload={"message_text": "普通批量动作"}),
                Action(id="action-source", tenant_id=1, task_id="task-search", task_type="search_join_group", action_type="search_join", account_id=12, status="pending", scheduled_at=now_value, payload={}),
            ]
        )
        session.commit()

        claimed = claim_actions(session, limit=1, worker_id="worker-search-source")

        assert [action.id for action in claimed] == ["action-source"]
        assert session.get(Action, "action-batch").status == "pending"


@pytest.mark.no_postgres
def test_due_actions_keeps_hard_target_before_search_membership_before_batch():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add_all(
            [
                Task(id="task-hard", tenant_id=1, name="硬目标", type="group_ai_chat", status="running", priority=9),
                Task(id="task-search", tenant_id=1, name="搜索准入", type="search_join_group", status="running", priority=3),
                Task(id="task-batch", tenant_id=1, name="普通批量", type="group_ai_chat", status="running", priority=1),
            ]
        )
        session.add_all(
            [
                Action(id="action-hard", tenant_id=1, task_id="task-hard", task_type="group_ai_chat", action_type="send_message", status="pending", scheduled_at=now_value, payload={"hard_hourly_target": True}),
                Action(id="action-membership", tenant_id=1, task_id="task-search", task_type="search_join_group", action_type="search_join_membership", status="pending", scheduled_at=now_value, payload={}),
                Action(id="action-batch", tenant_id=1, task_id="task-batch", task_type="group_ai_chat", action_type="send_message", status="pending", scheduled_at=now_value - timedelta(minutes=10), payload={}),
            ]
        )
        session.commit()

        actions = dispatcher.due_actions(session, limit=3)

        assert [action.id for action in actions] == ["action-hard", "action-membership", "action-batch"]


@pytest.mark.no_postgres
def test_due_actions_keeps_search_membership_before_strict_source_before_batch():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add_all(
            [
                Task(id="task-search", tenant_id=1, name="严格搜索点击", type="search_join_group", status="running", priority=3, type_config={"strict_daily_target": True, "daily_click_target_count": 500}),
                Task(id="task-batch", tenant_id=1, name="普通批量", type="group_ai_chat", status="running", priority=1),
            ]
        )
        session.add_all(
            [
                Action(id="action-membership", tenant_id=1, task_id="task-search", task_type="search_join_group", action_type="search_join_membership", status="pending", scheduled_at=now_value, payload={}),
                Action(id="action-source", tenant_id=1, task_id="task-search", task_type="search_join_group", action_type="search_join", status="pending", scheduled_at=now_value, payload={}),
                Action(id="action-batch", tenant_id=1, task_id="task-batch", task_type="group_ai_chat", action_type="send_message", status="pending", scheduled_at=now_value - timedelta(minutes=10), payload={}),
            ]
        )
        session.commit()

        actions = dispatcher.due_actions(session, limit=3)

        assert [action.id for action in actions] == ["action-membership", "action-source", "action-batch"]


@pytest.mark.no_postgres
def test_due_actions_keeps_task_priority_before_comment_rank():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add_all(
            [
                Task(id="task-priority", tenant_id=1, name="高优先级批量任务", type="channel_like", status="running", priority=1),
                Task(id="task-comment", tenant_id=1, name="普通评论", type="channel_comment", status="running", priority=3),
                Task(id="task-batch", tenant_id=1, name="普通批量任务", type="channel_like", status="running", priority=3),
            ]
        )
        session.add_all(
            [
                Action(id="action-priority", tenant_id=1, task_id="task-priority", task_type="channel_like", action_type="react_message", status="pending", scheduled_at=now_value),
                Action(id="action-comment", tenant_id=1, task_id="task-comment", task_type="channel_comment", action_type="ensure_target_membership", status="pending", scheduled_at=now_value),
                Action(id="action-batch", tenant_id=1, task_id="task-batch", task_type="channel_like", action_type="react_message", status="pending", scheduled_at=now_value - timedelta(minutes=10)),
            ]
        )
        session.commit()

        actions = dispatcher.due_actions(session, limit=3)

        assert [action.id for action in actions] == ["action-priority", "action-comment", "action-batch"]


def test_claim_actions_prioritizes_hard_hourly_membership_before_send(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()
    monkeypatch.setattr(dispatcher, "get_settings", lambda: _redis_bucket_settings(enable_redis_token_bucket=False))

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add_all(
            [
                TgAccount(id=11, tenant_id=1, display_name="账号A", phone_masked="+861***0011", status="在线"),
                TgAccount(id=12, tenant_id=1, display_name="账号B", phone_masked="+861***0012", status="在线"),
            ]
        )
        session.add(
            Task(
                id="task-hard",
                tenant_id=1,
                name="硬目标",
                type="group_ai_chat",
                status="running",
                priority=9,
                type_config={"hard_hourly_target_enabled": True, "hourly_min_messages": 300},
            )
        )
        session.add_all(
            [
                Action(
                    id="action-hard-send",
                    tenant_id=1,
                    task_id="task-hard",
                    task_type="group_ai_chat",
                    action_type="send_message",
                    account_id=12,
                    status="pending",
                    scheduled_at=now_value,
                    payload={"message_text": "hard", "hard_hourly_target": True},
                ),
                Action(
                    id="action-hard-membership",
                    tenant_id=1,
                    task_id="task-hard",
                    task_type="group_ai_chat",
                    action_type="ensure_target_membership",
                    account_id=11,
                    status="pending",
                    scheduled_at=now_value - timedelta(minutes=10),
                    payload={"channel_id": "-1007", "channel_target_id": 7, "target_type": "group", "require_send": True},
                ),
            ]
        )
        session.commit()

        claimed = claim_actions(session, limit=1, worker_id="worker-hard-hourly")

        assert [action.id for action in claimed] == ["action-hard-membership"]
        assert session.get(Action, "action-hard-send").status == "pending"


def test_claim_actions_does_not_starve_overdue_hard_hourly_send_behind_membership(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()
    monkeypatch.setattr(dispatcher, "get_settings", lambda: _redis_bucket_settings(enable_redis_token_bucket=False))

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add_all(
            [
                TgAccount(id=11, tenant_id=1, display_name="账号A", phone_masked="+861***0011", status="在线"),
                TgAccount(id=12, tenant_id=1, display_name="账号B", phone_masked="+861***0012", status="在线"),
            ]
        )
        task_config = {"hard_hourly_target_enabled": True, "hourly_min_messages": 300}
        session.add(
            Task(
                id="task-membership",
                tenant_id=1,
                name="补入群",
                type="group_ai_chat",
                status="running",
                priority=9,
                type_config=task_config,
            )
        )
        session.add(
            Task(
                id="task-send",
                tenant_id=1,
                name="补发言",
                type="group_ai_chat",
                status="running",
                priority=9,
                type_config=task_config,
            )
        )
        session.add_all(
            [
                Action(
                    id="action-hard-membership",
                    tenant_id=1,
                    task_id="task-membership",
                    task_type="group_ai_chat",
                    action_type="ensure_target_membership",
                    account_id=11,
                    status="pending",
                    scheduled_at=_now(),
                    payload={"channel_id": "-1007", "channel_target_id": 7, "target_type": "group", "require_send": True},
                ),
                Action(
                    id="action-hard-send",
                    tenant_id=1,
                    task_id="task-send",
                    task_type="group_ai_chat",
                    action_type="send_message",
                    account_id=12,
                    status="pending",
                    scheduled_at=now_value - timedelta(minutes=10),
                    payload={"message_text": "hard", "hard_hourly_target": True},
                ),
            ]
        )
        session.commit()

        claimed = claim_actions(session, limit=1, worker_id="worker-hard-hourly")

        assert [action.id for action in claimed] == ["action-hard-send"]
        assert session.get(Action, "action-hard-membership").status == "pending"


def test_claim_actions_ignores_overdue_hard_hourly_siblings_for_capacity(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()
    monkeypatch.setattr(dispatcher, "get_settings", lambda: _redis_bucket_settings(enable_redis_token_bucket=False))

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(SchedulingSetting(tenant_id=1, default_account_hour_limit=1, jitter_min_seconds=0, jitter_max_seconds=0))
        session.add(TgAccount(id=11, tenant_id=1, display_name="账号A", phone_masked="+861***0011", status="在线"))
        session.add(
            Task(
                id="task-hard",
                tenant_id=1,
                name="硬目标",
                type="group_ai_chat",
                status="running",
                priority=9,
                type_config={"hard_hourly_target_enabled": True, "hourly_min_messages": 300},
            )
        )
        session.add_all(
            [
                Action(
                    id="action-hard-a",
                    tenant_id=1,
                    task_id="task-hard",
                    task_type="group_ai_chat",
                    action_type="send_message",
                    account_id=11,
                    status="pending",
                    scheduled_at=now_value - timedelta(minutes=5),
                    payload={"message_text": "hard-a", "hard_hourly_target": True},
                ),
                Action(
                    id="action-hard-b",
                    tenant_id=1,
                    task_id="task-hard",
                    task_type="group_ai_chat",
                    action_type="send_message",
                    account_id=11,
                    status="pending",
                    scheduled_at=now_value - timedelta(minutes=4),
                    payload={"message_text": "hard-b", "hard_hourly_target": True},
                ),
            ]
        )
        session.commit()

        claimed = claim_actions(session, limit=1, worker_id="worker-hard-hourly")

        assert [action.id for action in claimed] == ["action-hard-a"]
        assert session.get(Action, "action-hard-b").status == "pending"


def test_claim_actions_skips_expired_hard_hourly_bucket_before_current_bucket(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()
    expired_bucket = (now_value - timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    current_bucket = now_value.replace(minute=0, second=0, microsecond=0)
    expired_bucket_payload = expired_bucket.replace(tzinfo=BEIJING_TZ)
    current_bucket_payload = current_bucket.replace(tzinfo=BEIJING_TZ)
    monkeypatch.setattr(dispatcher, "get_settings", lambda: _redis_bucket_settings(enable_redis_token_bucket=False))

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgAccount(id=11, tenant_id=1, display_name="账号A", phone_masked="+861***0011", status="在线"))
        session.add(TgAccount(id=12, tenant_id=1, display_name="账号B", phone_masked="+861***0012", status="在线"))
        session.add(
            Task(
                id="task-hard",
                tenant_id=1,
                name="硬目标",
                type="group_ai_chat",
                status="running",
                priority=9,
                type_config={"hard_hourly_target_enabled": True, "hourly_min_messages": 300},
            )
        )
        session.add_all(
            [
                Action(
                    id="action-expired-bucket",
                    tenant_id=1,
                    task_id="task-hard",
                    task_type="group_ai_chat",
                    action_type="send_message",
                    account_id=11,
                    status="pending",
                    scheduled_at=expired_bucket + timedelta(minutes=59),
                    payload={
                        "message_text": "expired",
                        "hard_hourly_target": True,
                        "hard_hourly_bucket": expired_bucket_payload.isoformat(),
                    },
                ),
                Action(
                    id="action-current-bucket",
                    tenant_id=1,
                    task_id="task-hard",
                    task_type="group_ai_chat",
                    action_type="send_message",
                    account_id=12,
                    status="pending",
                    scheduled_at=now_value,
                    payload={
                        "message_text": "current",
                        "hard_hourly_target": True,
                        "hard_hourly_bucket": current_bucket_payload.isoformat(),
                    },
                ),
            ]
        )
        session.commit()

        claimed = claim_actions(session, limit=2, worker_id="worker-hard-hourly")

        assert [action.id for action in claimed] == ["action-current-bucket"]
        expired = session.get(Action, "action-expired-bucket")
        assert expired.status == "skipped"
        assert expired.result["error_code"] == "hard_hourly_bucket_expired"


def test_recovery_skips_future_pending_expired_hard_hourly_bucket():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()
    expired_bucket = (now_value - timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    current_bucket = now_value.replace(minute=0, second=0, microsecond=0)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(Task(id="task-hard-recovery", tenant_id=1, name="硬目标", type="group_ai_chat", status="running"))
        session.add_all(
            [
                Action(
                    id="action-expired-future",
                    tenant_id=1,
                    task_id="task-hard-recovery",
                    task_type="group_ai_chat",
                    action_type="send_message",
                    account_id=11,
                    status="pending",
                    scheduled_at=now_value + timedelta(minutes=5),
                    payload={"message_text": "old", "hard_hourly_target": True, "hard_hourly_bucket": expired_bucket.isoformat()},
                ),
                Action(
                    id="action-current-future",
                    tenant_id=1,
                    task_id="task-hard-recovery",
                    task_type="group_ai_chat",
                    action_type="send_message",
                    account_id=12,
                    status="pending",
                    scheduled_at=now_value + timedelta(minutes=5),
                    payload={"message_text": "new", "hard_hourly_target": True, "hard_hourly_bucket": current_bucket.isoformat()},
                ),
            ]
        )
        session.commit()

        recovered = dispatcher.recover_expired_hard_hourly_actions(session)

        expired = session.get(Action, "action-expired-future")
        current = session.get(Action, "action-current-future")
        assert recovered == 1
        assert expired.status == "skipped"
        assert expired.result["error_code"] == "hard_hourly_bucket_expired"
        assert current.status == "pending"


def test_hard_hourly_recovery_uses_large_cleanup_batch():
    assert task_service._hard_hourly_recovery_limit(5) == 1000
    assert task_service._hard_hourly_recovery_limit(100) == 2000


def test_retry_skips_expired_hard_hourly_bucket_without_rescheduling(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = datetime(2026, 6, 14, 17, 5, 0)
    expired_bucket = datetime(2026, 6, 14, 16, 0, 0)
    current_bucket = datetime(2026, 6, 14, 17, 0, 0)
    monkeypatch.setattr("app.services.task_center.stats._now", lambda: now_value)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        task = Task(
            id="task-hard-retry",
            tenant_id=1,
            name="硬目标重试",
            type="group_ai_chat",
            status="running",
            failure_policy={"max_retries": 1, "retry_delay_seconds": 30, "retry_backoff": "none"},
        )
        session.add(task)
        session.add_all(
            [
                Action(
                    id="action-expired-retry",
                    tenant_id=1,
                    task_id=task.id,
                    task_type="group_ai_chat",
                    action_type="send_message",
                    status="failed",
                    retry_count=0,
                    scheduled_at=now_value - timedelta(minutes=20),
                    payload={"hard_hourly_target": True, "hard_hourly_bucket": expired_bucket.isoformat()},
                    result={"error_code": "execution_timeout"},
                ),
                Action(
                    id="action-current-retry",
                    tenant_id=1,
                    task_id=task.id,
                    task_type="group_ai_chat",
                    action_type="send_message",
                    status="failed",
                    retry_count=0,
                    scheduled_at=now_value - timedelta(minutes=1),
                    payload={"hard_hourly_target": True, "hard_hourly_bucket": current_bucket.isoformat()},
                    result={"error_code": "execution_timeout"},
                ),
            ]
        )
        session.commit()

        processed = retry_failed_actions(session, task)

        expired = session.get(Action, "action-expired-retry")
        current = session.get(Action, "action-current-retry")
        assert processed == 2
        assert expired.status == "skipped"
        assert expired.retry_count == 0
        assert expired.result["error_code"] == "hard_hourly_bucket_expired"
        assert expired.result["previous_result"]["error_code"] == "execution_timeout"
        assert current.status == "pending"
        assert current.retry_count == 1
        assert current.scheduled_at == now_value + timedelta(seconds=30)
        assert current.result["retry_scheduled"] is True


@pytest.mark.no_postgres
def test_retry_failed_actions_keeps_ai_quality_gate_failures_terminal(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = datetime(2026, 7, 9, 0, 40, 0)
    monkeypatch.setattr("app.services.task_center.stats._now", lambda: now_value)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        task = Task(
            id="task-ai-quality-terminal",
            tenant_id=1,
            name="AI质量门终态",
            type="group_ai_chat",
            status="running",
            failure_policy={"max_retries": 2, "retry_delay_seconds": 30},
        )
        session.add(task)
        session.add_all(
            [
                Action(
                    id="action-duplicate-terminal",
                    tenant_id=1,
                    task_id=task.id,
                    task_type="group_ai_chat",
                    action_type="send_message",
                    status="failed",
                    retry_count=0,
                    scheduled_at=now_value - timedelta(seconds=10),
                    payload={
                        "ai_generation_status": "duplicate_rejected",
                        "quality_skip_reason": "duplicate_message",
                        "message_text": "重复文案",
                        "ai_message_memory_id": "",
                    },
                    result={"error_code": "duplicate_message", "validation_stage": "ai_message_memory"},
                ),
                Action(
                    id="action-memory-missing-terminal",
                    tenant_id=1,
                    task_id=task.id,
                    task_type="group_ai_chat",
                    action_type="send_message",
                    status="failed",
                    retry_count=0,
                    scheduled_at=now_value - timedelta(seconds=5),
                    payload={
                        "ai_generation_status": "duplicate_rejected",
                        "quality_skip_reason": "duplicate_message",
                        "message_text": "缺少记忆预占",
                        "ai_message_memory_id": "",
                    },
                    result={"error_code": "ai_message_memory_missing", "validation_stage": "ai_message_memory"},
                ),
            ]
        )
        session.commit()

        assert retry_failed_actions(session, task) == 0

        duplicate = session.get(Action, "action-duplicate-terminal")
        memory_missing = session.get(Action, "action-memory-missing-terminal")
        assert duplicate.status == "failed"
        assert duplicate.retry_count == 0
        assert duplicate.result["error_code"] == "duplicate_message"
        assert memory_missing.status == "failed"
        assert memory_missing.retry_count == 0
        assert memory_missing.result["error_code"] == "ai_message_memory_missing"


@pytest.mark.no_postgres
def test_target_admission_retry_does_not_reschedule_unknown_after_send_actions(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = datetime(2026, 6, 26, 3, 20, 0)
    monkeypatch.setattr("app.services.task_center.stats._now", lambda: now_value)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        task = Task(
            id="task-admission-retry",
            tenant_id=1,
            name="天津准入重试",
            type="target_admission_retry",
            status="running",
        )
        session.add(task)
        session.add(
            Action(
                id="action-unknown-admission",
                tenant_id=1,
                task_id=task.id,
                task_type="target_admission_retry",
                action_type="ensure_target_membership",
                status="unknown_after_send",
                retry_count=0,
                scheduled_at=now_value - timedelta(minutes=10),
                executed_at=now_value - timedelta(minutes=5),
                result={"error_code": "unknown_after_send"},
            )
        )
        session.commit()

        assert retry_failed_actions(session, task) == 0

        action = session.get(Action, "action-unknown-admission")
        assert action.status == "unknown_after_send"
        assert action.retry_count == 0
        assert action.executed_at == now_value - timedelta(minutes=5)
        assert action.result["error_code"] == "unknown_after_send"


def test_hard_hourly_replacement_scan_uses_planned_deficit():
    action = Action(payload={"hard_hourly_target": True, "hard_hourly_deficit_at_plan": 300})
    task = Task(type_config={"hourly_min_messages": 120})

    assert dispatcher._replacement_scan_limit(action, task) == 300
    assert dispatcher._replacement_scan_limit(Action(payload={}), task) == 10


def test_select_task_accounts_is_unsharded_by_default(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    settings = _redis_bucket_settings(account_shard_total=2, account_shard_index=1)
    monkeypatch.setattr(account_pool, "get_settings", lambda: settings)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add_all(
            [
                TgAccount(id=10, tenant_id=1, display_name="账号A", phone_masked="+861***0010", status="在线"),
                TgAccount(id=11, tenant_id=1, display_name="账号B", phone_masked="+861***0011", status="在线"),
            ]
        )
        session.commit()

        accounts = account_pool.select_task_accounts(session, 1, {}, limit=10)

        assert [account.id for account in accounts] == [10, 11]


def test_select_task_accounts_can_enforce_current_shard(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    settings = _redis_bucket_settings(account_shard_total=2, account_shard_index=1)
    monkeypatch.setattr(account_pool, "get_settings", lambda: settings)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add_all(
            [
                TgAccount(id=10, tenant_id=1, display_name="账号A", phone_masked="+861***0010", status="在线"),
                TgAccount(id=11, tenant_id=1, display_name="账号B", phone_masked="+861***0011", status="在线"),
            ]
        )
        session.commit()

        accounts = account_pool.select_task_accounts(session, 1, {}, limit=10, enforce_shard=True)

        assert [account.id for account in accounts] == [11]


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


def test_claim_actions_reassignment_respects_future_cooldown_after_release(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()
    overdue_at = now_value - timedelta(minutes=5)
    fake_redis = FakeRedisTokenBucket(blocked_key="rate:global:tg_api", wait_seconds=9)
    monkeypatch.setattr(dispatcher, "get_settings", lambda: _redis_bucket_settings())
    monkeypatch.setattr(dispatcher, "_redis_client", lambda _redis_url: fake_redis)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(SchedulingSetting(tenant_id=1, default_account_cooldown_seconds=180, jitter_min_seconds=0, jitter_max_seconds=0))
        session.add_all(
            [
                TgAccount(id=11, tenant_id=1, display_name="原账号A", phone_masked="+861***0011", status="在线"),
                TgAccount(id=12, tenant_id=1, display_name="原账号B", phone_masked="+861***0012", status="在线"),
                TgAccount(id=13, tenant_id=1, display_name="候选账号A", phone_masked="+861***0013", status="在线"),
                TgAccount(id=14, tenant_id=1, display_name="候选账号B", phone_masked="+861***0014", status="在线"),
            ]
        )
        session.add(
            Task(
                id="task-overdue-reassign",
                tenant_id=1,
                name="overdue reassign",
                type="group_ai_chat",
                status="running",
                priority=1,
                account_config={"selection_mode": "manual", "account_ids": [11, 12, 13, 14], "max_concurrent": 4},
            )
        )
        session.add_all(
            [
                Action(
                    id="old-11",
                    tenant_id=1,
                    task_id="task-overdue-reassign",
                    task_type="group_ai_chat",
                    action_type="send_message",
                    account_id=11,
                    status="success",
                    scheduled_at=now_value - timedelta(minutes=1),
                    executed_at=now_value - timedelta(minutes=1),
                    payload={"chat_id": "-1001", "message_text": "old"},
                ),
                Action(
                    id="old-12",
                    tenant_id=1,
                    task_id="task-overdue-reassign",
                    task_type="group_ai_chat",
                    action_type="send_message",
                    account_id=12,
                    status="success",
                    scheduled_at=now_value - timedelta(minutes=1),
                    executed_at=now_value - timedelta(minutes=1),
                    payload={"chat_id": "-1001", "message_text": "old"},
                ),
                Action(
                    id="action-overdue-a",
                    tenant_id=1,
                    task_id="task-overdue-reassign",
                    task_type="group_ai_chat",
                    action_type="send_message",
                    account_id=11,
                    status="pending",
                    scheduled_at=overdue_at,
                    payload={"chat_id": "-1001", "message_text": "a"},
                    result={"claim_released_reason": "redis_token_bucket_limited"},
                ),
                Action(
                    id="action-overdue-b",
                    tenant_id=1,
                    task_id="task-overdue-reassign",
                    task_type="group_ai_chat",
                    action_type="send_message",
                    account_id=12,
                    status="pending",
                    scheduled_at=overdue_at,
                    payload={"chat_id": "-1001", "message_text": "b"},
                    result={"claim_released_reason": "redis_token_bucket_limited"},
                ),
            ]
        )
        session.commit()

        claimed = claim_actions(session, limit=2, worker_id="worker-test")

        first = session.get(Action, "action-overdue-a")
        second = session.get(Action, "action-overdue-b")
        assert claimed == []
        assert first.account_id == 13
        assert second.account_id == 14
        assert first.result["claim_released_reason"] == "redis_token_bucket_limited"
        assert second.result["claim_released_reason"] == "redis_token_bucket_limited"


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


def test_recovery_reprobes_unknown_target_membership_action(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(OperationTarget(id=7, tenant_id=1, title="青岛师范学院", target_type="group", tg_peer_id="@qdsfxy"))
        session.add(TgAccount(id=11, tenant_id=1, display_name="账号", phone_masked="+861***0011", status="在线", session_ciphertext="session"))
        session.add(Task(id="task-membership", tenant_id=1, name="retry", type="target_admission_retry", status="running", stats={}))
        session.add(
            Action(
                id="action-membership",
                tenant_id=1,
                task_id="task-membership",
                task_type="target_admission_retry",
                action_type="ensure_target_membership",
                account_id=11,
                status="executing",
                scheduled_at=now_value - timedelta(hours=1),
                lease_owner="worker-a",
                lease_expires_at=now_value - timedelta(minutes=1),
                payload={"channel_id": "@qdsfxy", "channel_target_id": 7, "target_type": "group", "require_send": True},
            )
        )
        session.add(
            ExecutionAttempt(
                id="attempt-membership",
                tenant_id=1,
                action_id="action-membership",
                worker_id="worker-a",
                attempt_no=1,
                status="gateway_call_started",
                gateway_call_started_at=now_value - timedelta(minutes=5),
            )
        )
        session.commit()

        monkeypatch.setattr(task_service, "credentials_for_account", lambda *args, **kwargs: object())
        monkeypatch.setattr(task_service.gateway, "probe_target_capabilities", lambda *args, **kwargs: OperationResult(True, detail="可发言"))

        assert _recover_stale_executing_actions(session, timeout_minutes=30) == 1

        action = session.get(Action, "action-membership")
        attempt = session.get(ExecutionAttempt, "attempt-membership")
        link = session.scalar(select(TgGroupAccount).where(TgGroupAccount.account_id == 11))
        assert action.status == "success"
        assert action.result["membership_status"] == "recovered_after_unknown"
        assert attempt.status == "success"
        assert link.can_send is True
        assert link.permission_label == "可发言"


def test_recovery_reprobes_existing_unknown_target_membership_action(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(OperationTarget(id=7, tenant_id=1, title="青岛师范学院", target_type="group", tg_peer_id="@qdsfxy"))
        session.add(TgAccount(id=11, tenant_id=1, display_name="账号", phone_masked="+861***0011", status="在线", session_ciphertext="session"))
        session.add(Task(id="task-membership", tenant_id=1, name="retry", type="target_admission_retry", status="running", stats={}))
        session.add(
            Action(
                id="action-membership",
                tenant_id=1,
                task_id="task-membership",
                task_type="target_admission_retry",
                action_type="ensure_target_membership",
                account_id=11,
                status="unknown_after_send",
                scheduled_at=now_value - timedelta(hours=1),
                executed_at=now_value - timedelta(minutes=5),
                payload={"channel_id": "@qdsfxy", "channel_target_id": 7, "target_type": "group", "require_send": True},
                result={"error_code": "unknown_after_send"},
            )
        )
        session.commit()

        monkeypatch.setattr(task_service, "credentials_for_account", lambda *args, **kwargs: object())
        monkeypatch.setattr(task_service.gateway, "probe_target_capabilities", lambda *args, **kwargs: OperationResult(True, detail="可发言"))

        assert _recover_stale_executing_actions(session, timeout_minutes=30) == 1

        action = session.get(Action, "action-membership")
        link = session.scalar(select(TgGroupAccount).where(TgGroupAccount.account_id == 11))
        assert action.status == "success"
        assert action.result["membership_status"] == "recovered_after_unknown"
        assert link.can_send is True


@pytest.mark.no_postgres
def test_recovery_limits_existing_unknown_membership_reprobe_by_account_and_target(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()
    calls: list[tuple[int, str]] = []

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(OperationTarget(id=7, tenant_id=1, title="青岛师范学院", target_type="group", tg_peer_id="@qdsfxy"))
        session.add(TgAccount(id=11, tenant_id=1, display_name="账号", phone_masked="+861***0011", status="在线", session_ciphertext="session"))
        session.add(Task(id="task-membership", tenant_id=1, name="retry", type="target_admission_retry", status="running", stats={}))
        for index in range(3):
            session.add(
                Action(
                    id=f"action-membership-{index}",
                    tenant_id=1,
                    task_id="task-membership",
                    task_type="target_admission_retry",
                    action_type="ensure_target_membership",
                    account_id=11,
                    status="unknown_after_send",
                    scheduled_at=now_value - timedelta(hours=1, minutes=index),
                    executed_at=now_value - timedelta(minutes=10 + index),
                    payload={"channel_id": "@qdsfxy", "channel_target_id": 7, "target_type": "group", "require_send": True},
                    result={"error_code": "unknown_after_send"},
                )
            )
        session.commit()

        def fake_probe(account_id, target_peer_id, *_args, **_kwargs):
            calls.append((account_id, target_peer_id))
            return OperationResult(False, detail="still unknown")

        monkeypatch.setattr(task_service, "credentials_for_account", lambda *args, **kwargs: object())
        monkeypatch.setattr(task_service.gateway, "probe_target_capabilities", fake_probe)

        assert _recover_stale_executing_actions(session, timeout_minutes=30) == 0

        assert calls == [(11, "@qdsfxy")]


def test_membership_prefers_existing_group_peer_over_stale_username(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()
    calls: list[str] = []
    probes: list[str] = []

    def fake_join(_account_id, channel_peer_id, *_args, **_kwargs):
        calls.append(channel_peer_id)
        if channel_peer_id == "@qdsfxy":
            return OperationResult(False, "失败", "未知错误", 'No user has "qdsfxy" as username')
        return OperationResult(True, detail="joined")

    def fake_probe(_account_id, target_peer_id, _target_type, *_args, **_kwargs):
        probes.append(target_peer_id)
        return OperationResult(True, detail="可发言")

    monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *args, **kwargs: object())
    monkeypatch.setattr(dispatcher.gateway, "ensure_channel_membership", fake_join)
    monkeypatch.setattr(dispatcher.gateway, "probe_target_capabilities", fake_probe)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(OperationTarget(id=7, tenant_id=1, title="青岛师范学院", target_type="group", tg_peer_id="@qdsfxy"))
        session.add(TgGroup(id=2149, tenant_id=1, title="青岛师范学院", tg_peer_id="-1002149", can_send=False))
        session.add(TgAccount(id=11, tenant_id=1, display_name="账号", phone_masked="+861***0011", status="在线", session_ciphertext="session"))
        session.add(Task(id="task-membership", tenant_id=1, name="retry", type="target_admission_retry", status="running", stats={}))
        session.add(
            Action(
                id="action-membership",
                tenant_id=1,
                task_id="task-membership",
                task_type="target_admission_retry",
                action_type="ensure_target_membership",
                account_id=11,
                status="pending",
                scheduled_at=now_value,
                payload={"channel_id": "@qdsfxy", "channel_target_id": 7, "target_type": "group", "target_display": "青岛师范学院", "require_send": True},
            )
        )
        session.commit()

        action = session.get(Action, "action-membership")
        assert dispatcher.dispatch_action(session, action) is True

        action = session.get(Action, "action-membership")
        target = session.get(OperationTarget, 7)
        link = session.scalar(select(TgGroupAccount).where(TgGroupAccount.account_id == 11))
        assert calls == ["-1002149"]
        assert probes == ["-1002149"]
        assert action.status == "success"
        assert action.result["membership_fallback_ref"] == "-1002149"
        assert action.result["membership_peer_ref"] == "-1002149"
        assert "target_peer_updated" not in action.result
        assert target.tg_peer_id == "@qdsfxy"
        assert link.group_id == 2149
        assert link.can_send is True


def test_membership_prefers_stable_peer_before_username_for_send_required_join(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()
    calls: list[str] = []

    def fake_join(_account_id, channel_peer_id, *_args, **_kwargs):
        calls.append(channel_peer_id)
        return OperationResult(True, detail="joined")

    monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *args, **kwargs: object())
    monkeypatch.setattr(dispatcher.gateway, "ensure_channel_membership", fake_join)
    monkeypatch.setattr(dispatcher.gateway, "probe_target_capabilities", lambda *_args, **_kwargs: OperationResult(True, detail="可发言"))

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(OperationTarget(id=7, tenant_id=1, title="青岛师范学院", target_type="group", tg_peer_id="-1002149", username="qdsfxy"))
        session.add(TgAccount(id=11, tenant_id=1, display_name="账号", phone_masked="+861***0011", status="在线", session_ciphertext="session"))
        session.add(Task(id="task-membership", tenant_id=1, name="retry", type="target_admission_retry", status="running", stats={}))
        session.add(
            Action(
                id="action-membership",
                tenant_id=1,
                task_id="task-membership",
                task_type="target_admission_retry",
                action_type="ensure_target_membership",
                account_id=11,
                status="pending",
                scheduled_at=now_value,
                payload={"channel_id": "-1002149", "channel_target_id": 7, "target_type": "group", "target_display": "青岛师范学院", "target_username": "qdsfxy", "require_send": True},
            )
        )
        session.commit()

        action = session.get(Action, "action-membership")
        assert dispatcher.dispatch_action(session, action) is True

        assert calls == ["-1002149"]
        assert session.get(Action, "action-membership").status == "success"


def test_group_ai_build_plan_canonicalizes_duplicate_username_target_before_membership_gate(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()
    monkeypatch.setattr(group_ai_chat, "_now", lambda: now_value)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(OperationTarget(id=485, tenant_id=1, title="天津", target_type="group", tg_peer_id="-1003583171851", username="zzjinli", can_send=True, auth_status="已授权运营"))
        session.add(OperationTarget(id=1251, tenant_id=1, title="zzjinli", target_type="group", tg_peer_id="zzjinli", username="zzjinli", can_send=True, auth_status="已授权运营"))
        session.add(TgAccount(id=11, tenant_id=1, display_name="账号", phone_masked="+861***0011", status="在线", session_ciphertext="session"))
        task = Task(
            id="8ab323c9-tianjin",
            tenant_id=1,
            name="天津",
            type="group_ai_chat",
            status="running",
            account_config={"selection_mode": "manual", "account_ids": [11]},
            type_config={"target_operation_target_id": 1251},
            stats={},
        )
        session.add(task)
        session.commit()

        assert group_ai_chat.build_plan(session, task) == 1
        action = session.scalar(select(Action).where(Action.task_id == task.id, Action.action_type == "ensure_target_membership"))

    assert task.type_config["target_operation_target_id"] == 485
    assert task.type_config["target_group_name"] == "天津"
    assert action.payload["channel_target_id"] == 485
    assert action.payload["channel_id"] == "-1003583171851"
    assert action.payload["target_username"] == "zzjinli"


@pytest.mark.no_postgres
def test_group_ai_build_plan_does_not_reconcile_missing_online_state(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()
    monkeypatch.setattr(group_ai_chat, "_now", lambda: now_value)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="运营群", auth_status="已授权运营", can_send=True))
        session.add_all(
            [
                TgAccount(id=11, tenant_id=1, display_name="账号A", phone_masked="+861***0011", status="在线", session_ciphertext="session-a", proxy_id=5),
                TgAccount(id=12, tenant_id=1, display_name="账号B", phone_masked="+861***0012", status="在线", session_ciphertext="session-b"),
                TgAccount(id=99, tenant_id=1, display_name="全局保活", phone_masked="+861***0099", status="在线", session_ciphertext="session-global"),
            ]
        )
        session.add(
            TgAccountOnlineState(
                tenant_id=1,
                account_id=99,
                desired_online=True,
                desired_sources=[{"source_type": "global", "source_id": "global_keepalive"}],
                online_status="online",
                stale_after_at=now_value + timedelta(minutes=5),
            )
        )
        session.add_all(
            [
                TgGroupAccount(tenant_id=1, group_id=7, account_id=11, can_send=True),
                TgGroupAccount(tenant_id=1, group_id=7, account_id=12, can_send=True),
            ]
        )
        task = Task(
            id="task-online-reconcile",
            tenant_id=1,
            name="在线回填",
            type="group_ai_chat",
            status="running",
            account_config={"selection_mode": "all", "max_concurrent": 10, "cooldown_per_account_minutes": 0},
            type_config={"target_group_id": 7, "messages_per_round_mode": "manual", "messages_per_round": 2},
            stats={},
        )
        session.add(task)
        session.commit()

        assert group_ai_chat.build_plan(session, task) == 0
        states = list(session.scalars(select(TgAccountOnlineState).order_by(TgAccountOnlineState.account_id)))

        assert [state.account_id for state in states] == [99]
        assert states[0].desired_sources == [{"source_type": "global", "source_id": "global_keepalive"}]
        assert task.stats["account_offline_count"] == 2
        assert task.stats["account_offline_sample_account_ids"] == [11, 12]
        assert "在线状态不可用" in (task.last_error or "")


@pytest.mark.no_postgres
def test_group_ai_build_plan_writes_topic_teacher_and_burst_payload(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()
    monkeypatch.setattr(group_ai_chat, "_now", lambda: now_value)
    monkeypatch.setattr(group_ai_chat.random, "random", lambda: 0.0)
    monkeypatch.setattr(group_ai_chat.random, "randint", lambda _start, _end: 2)

    _forbid_planner_ai_generation(monkeypatch)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(
            TenantLearningProfile(
                tenant_id=1,
                profile_version=3,
                status="active",
                style_summary="群友偏短句追问，少总结",
                source_sample_count=8,
            )
        )
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="运营群", auth_status="已授权运营", can_send=True))
        session.add_all(
            [
                TgAccount(id=11, tenant_id=1, display_name="账号A", phone_masked="+861***0011", status="在线", session_ciphertext="session-a"),
                TgAccount(id=12, tenant_id=1, display_name="账号B", phone_masked="+861***0012", status="在线", session_ciphertext="session-b"),
            ]
        )
        session.add_all(
            [
                TgGroupAccount(tenant_id=1, group_id=7, account_id=11, can_send=True),
                TgGroupAccount(tenant_id=1, group_id=7, account_id=12, can_send=True),
            ]
        )
        session.add_all(
            [
                TgAccountOnlineState(tenant_id=1, account_id=11, desired_online=True, online_status="online", stale_after_at=now_value + timedelta(minutes=5)),
                TgAccountOnlineState(tenant_id=1, account_id=12, desired_online=True, online_status="online", stale_after_at=now_value + timedelta(minutes=5)),
                AiAccountVoiceProfile(
                    tenant_id=1,
                    account_id=11,
                    version=2,
                    short_prompt_summary="青年短句，爱追问价格，少表情",
                    quality_status="active",
                    status="active",
                ),
                AiAccountVoiceProfile(
                    tenant_id=1,
                    account_id=12,
                    version=1,
                    short_prompt_summary="中年中句，谨慎补经历，偶尔轻吐槽",
                    quality_status="active",
                    status="active",
                ),
                AiAccountGroupStanceMemory(
                    tenant_id=1,
                    group_id=7,
                    account_id=11,
                    summary="刚围绕王老师表达过观望，别突然强夸",
                ),
            ]
        )
        task = Task(
            id="task-topic-teacher-burst",
            tenant_id=1,
            name="话题老师连发",
            type="group_ai_chat",
            status="running",
            account_config={"selection_mode": "all", "max_concurrent": 10, "cooldown_per_account_minutes": 0},
            pacing_config={"max_actions_per_hour": 120},
            type_config={
                "target_group_id": 7,
                "messages_per_round_mode": "manual",
                "messages_per_round": 4,
                "reply_min_per_round": 0,
                "allow_account_repeat": True,
                "silent_mode_enabled": False,
                "fact_anchor_required": False,
                "low_confidence_silence_enabled": False,
                "topic_directions": [{"title": "升学规划", "description": "围绕择校节奏聊", "weight": 1}],
                "teacher_targets": [{"name": "王老师", "description": "负责报名答疑", "priority": 10}],
                "consecutive_message_enabled": True,
                "consecutive_message_min": 2,
                "consecutive_message_max": 2,
                "consecutive_message_probability": 1,
            },
            stats={},
        )
        session.add(task)
        session.commit()

        assert group_ai_chat.build_plan(session, task) == 4
        actions = list(session.scalars(select(Action).where(Action.task_id == task.id).order_by(Action.scheduled_at, Action.created_at)))

    assert [action.account_id for action in actions[:2]] == [11, 11]
    assert actions[0].payload["burst_id"] == actions[1].payload["burst_id"]
    assert actions[0].payload["burst_index"] == 1
    assert actions[1].payload["burst_index"] == 2
    assert actions[0].payload["burst_size"] == 2
    assert actions[0].payload["topic_direction"]["title"] == "升学规划"
    assert actions[0].payload["teacher_target"]["name"] == "王老师"
    assert actions[0].payload["slot_id"] == "task-topic-teacher-burst:cycle:1:turn:1"
    assert actions[0].payload["act_type"]
    assert actions[0].payload["account_voice_profile_version"] == 2
    assert actions[0].payload["account_voice_profile_summary"] == "青年短句，爱追问价格，少表情"
    assert actions[0].payload["stance_summary"] == "刚围绕王老师表达过观望，别突然强夸"
    assert actions[0].payload["profile_version"] == 3
    assert actions[0].payload["profile_match_score"] == 100
    assert actions[0].payload["profile_match_reason"] == "群友偏短句追问，少总结"
    assert all(action.payload["ai_generation_status"] == "pending" for action in actions)
    assert all(not action.payload["message_text"] for action in actions)
    assert all(not action.payload["ai_message_memory_id"] for action in actions)


@pytest.mark.no_postgres
def test_group_ai_expires_open_actions_without_voice_profile_before_replan():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()

    with Session(engine) as session:
        task = Task(id="task-replan-profile", tenant_id=1, name="活群", type="group_ai_chat", status="running")
        memory = AiGroupMessageMemory(
            id="memory-old-profileless",
            tenant_id=1,
            group_id=7,
            task_id=task.id,
            account_id=11,
            raw_text="旧文案",
            status="reserved",
        )
        profileless = Action(
            id="action-old-profileless",
            tenant_id=1,
            task_id=task.id,
            task_type="group_ai_chat",
            action_type="send_message",
            account_id=11,
            status="pending",
            scheduled_at=now_value,
            payload={"message_text": "旧文案", "ai_message_memory_id": memory.id},
        )
        retryable_profileless = Action(
            id="action-retryable-profileless",
            tenant_id=1,
            task_id=task.id,
            task_type="group_ai_chat",
            action_type="send_message",
            account_id=11,
            status="retryable_failed",
            scheduled_at=now_value,
            payload={"message_text": "旧失败文案"},
        )
        profiled = Action(
            id="action-profiled",
            tenant_id=1,
            task_id=task.id,
            task_type="group_ai_chat",
            action_type="send_message",
            account_id=11,
            status="pending",
            scheduled_at=now_value,
            payload={"message_text": "新文案", "account_mask_version": 2},
        )
        session.add_all([task, memory, profileless, retryable_profileless, profiled])
        session.commit()

        expired = group_ai_chat._expire_open_profileless_actions(session, task, [11])
        session.flush()

        assert expired == 2
        assert profileless.status == "skipped"
        assert retryable_profileless.status == "skipped"
        assert profileless.result["error_code"] == "voice_profile_replan"
        assert memory.status == "expired_before_send"
        assert profiled.status == "pending"
        assert task.stats["voice_profile_replanned_open_action_count"] == 2


@pytest.mark.no_postgres
def test_group_ai_build_plan_keeps_fixed_pending_slot_accounts(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()
    monkeypatch.setattr(group_ai_chat, "_now", lambda: now_value)
    _forbid_planner_ai_generation(monkeypatch)
    with Session(engine) as session:
        task = seed_ai_planner_scope(
            session,
            now_value,
            AiPlannerScenario(
                task_id="task-slot-first",
                task_name="slot first",
                profile_summaries=("青年短句，偶尔表情", "中年短句，轻吐槽"),
                messages_per_round=2,
            ),
        )
        assert group_ai_chat.build_plan(session, task) == 2
        actions = list(session.scalars(select(Action).where(Action.task_id == task.id).order_by(Action.payload["turn_index"])))
    assert [action.account_id for action in actions] == [11, 12]
    assert [action.payload["slot_id"] for action in actions] == ["task-slot-first:cycle:1:turn:1", "task-slot-first:cycle:1:turn:2"]
    assert [action.payload["act_type"] for action in actions] == ["short_react", "detail_follow"]
    assert [action.payload["generation_source"] for action in actions] == ["bootstrap", "bootstrap"]
    assert [action.payload["ai_generation_status"] for action in actions] == ["pending", "pending"]


@pytest.mark.no_postgres
def test_group_ai_planner_defers_quality_retry_to_dispatcher(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()
    monkeypatch.setattr(group_ai_chat, "_now", lambda: now_value)
    generated_configs: list[dict] = []
    rounds = iter([["😀😀"], ["花花老师价格大概多少"], ["主任最近约新妹子了吗"]])

    def fake_generate(_session, _tenant_id, config, *, count, target_label, history):  # noqa: ANN001
        generated_configs.append(dict(config))
        return _slot_bound_contents(config, next(rounds)[:count]), 5

    with Session(engine) as session:
        task = seed_ai_planner_scope(
            session,
            now_value,
            AiPlannerScenario(
                task_id="task-slot-retry",
                task_name="slot retry",
                profile_summaries=("青年短句，爱问价格，少表情", "中年短句，接话补充"),
                messages_per_round=2,
                tenant_ai_enabled=True,
                emoji_policy="少表情，不连续发表情",
                include_previous_photo_memory=True,
            ),
        )
        session.get(TgGroup, 7).group_cooldown_seconds = 0
        session.commit()
        assert group_ai_chat.build_plan(session, task) == 2
        actions = list(session.scalars(select(Action).where(
            Action.task_id == task.id,
            Action.status == "pending",
        ).order_by(Action.payload["turn_index"])))
        assert [action.payload["ai_generation_status"] for action in actions] == ["pending", "pending"]
        _dispatch_planned_ai_actions(session, monkeypatch, actions, normal_generator=fake_generate)
        action_states = [
            (action.account_id, action.status, dict(action.payload or {}), dict(action.result or {}))
            for action in actions
        ]
    assert_quality_retry_states(generated_configs, action_states)


@pytest.mark.no_postgres
def test_group_ai_build_plan_blocks_missing_voice_profile(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()
    monkeypatch.setattr(group_ai_chat, "_now", lambda: now_value)

    _forbid_planner_ai_generation(monkeypatch)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="运营群", auth_status="已授权运营", can_send=True))
        session.add(TgAccount(id=11, tenant_id=1, display_name="账号A", phone_masked="+861***0011", status="在线", session_ciphertext="session-a"))
        session.add(TgGroupAccount(tenant_id=1, group_id=7, account_id=11, can_send=True))
        session.add(TgAccountOnlineState(tenant_id=1, account_id=11, desired_online=True, online_status="online", stale_after_at=now_value + timedelta(minutes=5)))
        task = Task(
            id="task-missing-voice-profile",
            tenant_id=1,
            name="缺面具校验",
            type="group_ai_chat",
            status="running",
            account_config={"selection_mode": "all", "max_concurrent": 1, "cooldown_per_account_minutes": 0},
            pacing_config={"max_actions_per_hour": 120},
            type_config={
                "target_group_id": 7,
                "messages_per_round_mode": "manual",
                "messages_per_round": 1,
                "reply_min_per_round": 0,
                "silent_mode_enabled": False,
                "fact_anchor_required": False,
                "low_confidence_silence_enabled": False,
            },
            stats={},
        )
        session.add(task)
        session.commit()

        assert group_ai_chat.build_plan(session, task) == 0
        actions = list(session.scalars(select(Action).where(Action.task_id == task.id)))
        session.refresh(task)

    assert actions == []
    assert task.last_error == group_ai_chat.VOICE_PROFILE_MISSING_MESSAGE
    assert task.stats["voice_profile_missing_count"] == 1
    assert task.stats["quality_rejection_counts"]["voice_profile_missing"] == 1


@pytest.mark.no_postgres
def test_group_ai_planner_defers_voice_profile_match_to_dispatcher(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()
    monkeypatch.setattr(group_ai_chat, "_now", lambda: now_value)

    def fake_generate(_session, _tenant_id, config, **_kwargs):  # noqa: ANN001
        return _slot_bound_contents(config, ["😀😀"]), 3

    with Session(engine) as session:
        task = seed_ai_planner_scope(
            session,
            now_value,
            AiPlannerScenario(
                task_id="task-voice-profile-mismatch",
                task_name="面具校验",
                profile_summaries=("青年短句，少表情，不连续发表情",),
                emoji_policy="少表情，不连续发表情",
                profile_versions=(2,),
                max_concurrent=1,
                include_allow_repeat=False,
            ),
        )
        assert group_ai_chat.build_plan(session, task) == 1
        actions = list(session.scalars(select(Action).where(Action.task_id == task.id)))
        assert actions[0].payload["ai_generation_status"] == "pending"
        _dispatch_planned_ai_actions(
            session,
            monkeypatch,
            actions,
            normal_generator=fake_generate,
        )
        session.refresh(task)

    assert len(actions) == 1
    assert actions[0].status == "failed"
    assert actions[0].result["error_code"] == "voice_profile_mismatch"
    assert actions[0].payload["ai_generation_status"] == "voice_profile_mismatch"
    assert actions[0].payload["message_text"] == ""


@pytest.mark.no_postgres
def test_group_ai_planner_defers_stance_check_to_dispatcher(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()
    monkeypatch.setattr(group_ai_chat, "_now", lambda: now_value)

    def fake_generate(_session, _tenant_id, config, **_kwargs):  # noqa: ANN001
        return _slot_bound_contents(config, ["王老师这个绝对可以，闭眼冲就行"]), 3

    with Session(engine) as session:
        task = seed_ai_planner_scope(
            session,
            now_value,
            AiPlannerScenario(
                task_id="task-stance-conflict",
                task_name="立场冲突",
                profile_summaries=("青年短句，谨慎接话",),
                stance_summary="刚围绕王老师表达过观望，别突然强夸",
                max_concurrent=1,
                include_allow_repeat=False,
            ),
        )
        assert group_ai_chat.build_plan(session, task) == 1
        actions = list(session.scalars(select(Action).where(Action.task_id == task.id)))
        assert actions[0].payload["ai_generation_status"] == "pending"
        _dispatch_planned_ai_actions(
            session,
            monkeypatch,
            actions,
            normal_generator=fake_generate,
        )
        session.refresh(task)

    assert len(actions) == 1
    assert actions[0].status == "failed"
    assert actions[0].result["error_code"] == "stance_conflict"
    assert actions[0].payload["ai_generation_status"] == "stance_conflict"
    assert actions[0].payload["stance_summary"] == "刚围绕王老师表达过观望，别突然强夸"


@pytest.mark.no_postgres
def test_group_ai_build_plan_deprioritizes_recent_topic_and_teacher(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()
    monkeypatch.setattr(group_ai_chat, "_now", lambda: now_value)

    _forbid_planner_ai_generation(monkeypatch)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="运营群", auth_status="已授权运营", can_send=True))
        session.add(TgAccount(id=11, tenant_id=1, display_name="账号A", phone_masked="+861***0011", status="在线", session_ciphertext="session-a"))
        session.add(TgGroupAccount(tenant_id=1, group_id=7, account_id=11, can_send=True))
        session.add(_voice_profile(11, "青年短句，按话题接话"))
        session.add(
            TgAccountOnlineState(
                tenant_id=1,
                account_id=11,
                desired_online=True,
                online_status="online",
                stale_after_at=now_value + timedelta(minutes=5),
            )
        )
        task = Task(
            id="task-topic-teacher-rotation",
            tenant_id=1,
            name="话题老师轮换",
            type="group_ai_chat",
            status="running",
            account_config={"selection_mode": "all", "max_concurrent": 10, "cooldown_per_account_minutes": 0},
            type_config={
                "target_group_id": 7,
                "messages_per_round_mode": "manual",
                "messages_per_round": 1,
                "fact_anchor_required": False,
                "low_confidence_silence_enabled": False,
                "topic_directions": [
                    {"title": "升学规划", "weight": 10},
                    {"title": "资料准备", "weight": 1},
                ],
                "teacher_targets": [
                    {"name": "王老师", "priority": 10},
                    {"name": "李老师", "priority": 1},
                ],
            },
            stats={"force_bootstrap_once": True},
        )
        session.add(task)
        session.add(
            AiGroupMessageMemory(
                tenant_id=1,
                group_id=7,
                task_id=task.id,
                account_id=11,
                topic_direction="升学规划",
                teacher_target="王老师",
                raw_text="王老师之前聊过升学规划",
                normalized_text="王老师之前聊过升学规划",
                text_fingerprint="recent-topic-teacher-memory",
                semantic_cluster="recent-topic-teacher",
                status="success",
                planned_at=now_value - timedelta(minutes=3),
                sent_at=now_value - timedelta(minutes=3),
            )
        )
        session.add(
            Action(
                id="recent-topic-teacher",
                tenant_id=1,
                task_id=task.id,
                task_type="group_ai_chat",
                action_type="send_message",
                status="success",
                account_id=11,
                payload={
                    "group_id": 7,
                    "message_text": "王老师之前聊过升学规划",
                    "topic_direction": {"title": "升学规划"},
                    "teacher_target": {"name": "王老师"},
                },
                created_at=now_value - timedelta(minutes=3),
                executed_at=now_value - timedelta(minutes=3),
            )
        )
        session.commit()

        created = group_ai_chat.build_plan(session, task)
        assert created == 1, f"last_error={task.last_error!r} stats={task.stats!r}"
        action = session.scalar(select(Action).where(Action.task_id == task.id, Action.id != "recent-topic-teacher"))

    assert action is not None
    assert action.payload["topic_direction"]["title"] == "资料准备"
    assert action.payload["teacher_target"]["name"] == "李老师"


@pytest.mark.no_postgres
def test_group_ai_planner_defers_memory_duplicate_check_to_dispatcher(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()
    monkeypatch.setattr(group_ai_chat, "_now", lambda: now_value)

    def fake_generate(_session, _tenant_id, config, **_kwargs):  # noqa: ANN001
        return _slot_bound_contents(config, ["花花老师身材服务真好"]), 5

    with Session(engine) as session:
        task = seed_ai_planner_scope(
            session,
            now_value,
            AiPlannerScenario(
                task_id="task-memory-duplicate",
                task_name="记忆重复",
                profile_summaries=("青年短句，少总结",),
                force_bootstrap=True,
                include_pacing=False,
                include_reply_min=False,
                include_allow_repeat=False,
                include_silent_mode=False,
                include_low_confidence=False,
            ),
        )
        seed_sent_memory(session, now_value, text="花花老师身材服务真好")
        assert group_ai_chat.build_plan(session, task) == 1
        action = session.scalar(select(Action).where(Action.task_id == task.id))
        assert action.payload["ai_generation_status"] == "pending"
        _dispatch_planned_ai_actions(
            session,
            monkeypatch,
            [action],
            normal_generator=fake_generate,
        )

    assert action is not None
    assert action.status == "failed"
    assert action.result["error_code"] == "duplicate_message"
    assert action.payload["ai_generation_status"] == "duplicate_message"
    assert action.payload["message_text"] == "花花老师身材服务真好"


@pytest.mark.no_postgres
def test_group_ai_reply_target_pool_excludes_targets_used_by_other_tasks():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        group = TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="运营群", auth_status="已授权运营", can_send=True)
        session.add(group)
        task = Task(id="task-current", tenant_id=1, name="当前任务", type="group_ai_chat", status="running")
        session.add(task)
        session.add(
            Action(
                id="other-task-reply",
                tenant_id=1,
                task_id="task-other",
                task_type="group_ai_chat",
                action_type="send_message",
                status="success",
                payload={"group_id": 7, "reply_to_message_id": 7001, "message_text": "这句接过了"},
                created_at=now_value - timedelta(minutes=3),
                executed_at=now_value - timedelta(minutes=3),
            )
        )
        context_row = GroupContextMessage(
            tenant_id=1,
            group_id=7,
            listener_account_id=11,
            sender_peer_id="9001",
            sender_name="真人A",
            content="停车位快没了",
            message_type="text",
            remote_message_id="7001",
            sent_at=now_value,
        )
        session.add(context_row)
        session.commit()

        targets = group_ai_chat._group_reply_target_pool(session, task, group, [context_row])

    assert targets == []


@pytest.mark.no_postgres
def test_group_ai_context_bound_round_limits_far_future_actions(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = datetime(2026, 6, 29, 4, 0)
    monkeypatch.setattr(group_ai_chat, "_now", lambda: now_value)
    monkeypatch.setattr("app.services.account_online_state._now", lambda: now_value)
    _forbid_planner_ai_generation(monkeypatch)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="运营群", auth_status="已授权运营", can_send=True))
        for account_id in range(100, 120):
            session.add(
                TgAccount(
                    id=account_id,
                    tenant_id=1,
                    display_name=f"账号{account_id}",
                    phone_masked=str(account_id),
                    status="在线",
                    session_ciphertext=f"session-{account_id}",
                )
            )
            session.add(TgGroupAccount(tenant_id=1, group_id=7, account_id=account_id, can_send=True))
            session.add(
                TgAccountOnlineState(
                    tenant_id=1,
                    account_id=account_id,
                    desired_online=True,
                    online_status="online",
                    stale_after_at=now_value + timedelta(minutes=5),
                )
            )
            session.add(_voice_profile(account_id, f"账号{account_id}短句，接真人上下文"))
        session.add(
            GroupContextMessage(
                tenant_id=1,
                group_id=7,
                listener_account_id=100,
                sender_name="真人",
                content="刚才说的天津那个咋样",
                message_type="text",
                remote_message_id="context-current",
                sent_at=now_value - timedelta(minutes=1),
            )
        )
        task = Task(
            id="task-context-near-term",
            tenant_id=1,
            name="上下文近端",
            type="group_ai_chat",
            status="running",
            account_config={"selection_mode": "all", "max_concurrent": 100, "cooldown_per_account_minutes": 0},
            pacing_config={
                "operation_profile": {
                    "hourly_activity_curve": [2, 2, 1, 1, 0, 0, 1, 2, 4, 5, 6, 6, 5, 4, 6, 7, 8, 9, 10, 10, 8, 6, 4, 3],
                    "quiet_threshold": 2,
                    "peak_threshold": 8,
                },
            },
            type_config={
                "target_group_id": 7,
                "messages_per_round_mode": "manual",
                "messages_per_round": 60,
                "participation_rate": 1,
                "participation_jitter": 0,
                "allow_account_repeat": True,
                "context_expire_after_messages": 1,
                "fact_anchor_required": False,
                "low_confidence_silence_enabled": False,
            },
            stats={},
        )
        session.add(task)
        session.commit()

        created = group_ai_chat.build_plan(session, task)
        actions = list(session.scalars(select(Action).where(Action.task_id == task.id).order_by(Action.scheduled_at.asc())))
        refreshed_task = session.get(Task, task.id)

    assert 1 <= created < 60
    assert len(actions) == created
    assert max(action.scheduled_at for action in actions) <= now_value + timedelta(minutes=5)
    assert {action.payload["context_snapshot_message_id"] for action in actions}
    assert refreshed_task.stats["context_bound_requested_turns"] == 60
    assert refreshed_task.stats["context_bound_planned_turns"] == created
    assert refreshed_task.stats["context_bound_schedule_window_seconds"] == 300


@pytest.mark.no_postgres
def test_group_ai_context_bound_limit_does_not_cap_hard_hourly(monkeypatch):
    now_value = datetime(2026, 6, 29, 4, 0)
    monkeypatch.setattr(group_ai_chat, "_now", lambda: now_value)
    task = Task(id="task-hard-context", tenant_id=1, name="硬目标", type="group_ai_chat", stats={})
    planned_times = [now_value + timedelta(minutes=index * 10) for index in range(12)]

    turn_count, limited_times = group_ai_chat._limit_context_bound_turns(
        task,
        {"context_expire_after_messages": 1},
        has_context=True,
        progress={"deficit": 12},
        turn_count=12,
        planned_times=planned_times,
    )

    assert turn_count == 12
    assert limited_times == planned_times
    assert "context_bound_requested_turns" not in (task.stats or {})


@pytest.mark.no_postgres
def test_group_ai_context_bound_limit_does_not_cap_deferred_daily_coverage(monkeypatch):
    now_value = datetime(2026, 6, 29, 20, 0)
    monkeypatch.setattr(group_ai_chat, "_now", lambda: now_value)
    task = Task(id="task-deferred-coverage-context", tenant_id=1, name="覆盖延期生成", type="group_ai_chat", stats={})
    planned_times = [now_value + timedelta(minutes=index * 2) for index in range(30)]

    turn_count, limited_times = group_ai_chat._limit_context_bound_turns(
        task,
        {"context_expire_after_messages": 1},
        has_context=True,
        progress={},
        deferred_generation=True,
        turn_count=30,
        planned_times=planned_times,
    )

    assert turn_count == 30
    assert limited_times == planned_times
    assert "context_bound_requested_turns" not in (task.stats or {})


@pytest.mark.no_postgres
def test_group_ai_context_bound_quality_schedule_cuts_final_candidates(monkeypatch):
    now_value = datetime(2026, 6, 29, 15, 0)
    monkeypatch.setattr(group_ai_chat, "_now", lambda: now_value)
    task = Task(
        id="task-context-quality",
        tenant_id=1,
        name="最终候选裁剪",
        type="group_ai_chat",
        stats={"context_bound_requested_turns": 20},
    )
    quality_items = [{"content": f"候选{i}"} for i in range(3)]
    planned_times = [now_value, now_value + timedelta(minutes=20), now_value + timedelta(hours=2)]

    limited_items, limited_times = group_ai_chat._limit_context_bound_quality_schedule(
        task,
        {"context_expire_after_messages": 1, "context_bound_schedule_window_seconds": 3600},
        has_context=True,
        progress={},
        quality_items=quality_items,
        planned_times=planned_times,
    )

    assert limited_items == quality_items[:2]
    assert limited_times == planned_times[:2]
    assert task.stats["context_bound_requested_turns"] == 20
    assert task.stats["context_bound_planned_turns"] == 2


def _add_ready_daily_coverage(
    session: Session,
    task: Task,
    account_ids: list[int],
    *,
    coverage_date: date | None = None,
) -> None:
    session.add_all([
        TaskAccountDailyCoverage(
            tenant_id=task.tenant_id,
            task_id=task.id,
            group_id=int(task.type_config["target_group_id"]),
            account_id=account_id,
            coverage_date=coverage_date or date.today(),
            state="ready",
            targeted_at=datetime.combine(coverage_date or date.today(), datetime.min.time()),
        )
        for account_id in account_ids
    ])


@pytest.mark.no_postgres
def test_group_ai_all_account_coverage_defers_plain_ai_without_emoji_fallback(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = datetime(2026, 7, 13, 23, 30)
    monkeypatch.setattr(group_ai_chat, "_now", lambda: now_value)
    monkeypatch.setattr(group_ai_chat.random, "sample", lambda pool, k: list(pool)[:k])
    _forbid_planner_ai_generation(monkeypatch)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TenantAiSetting(tenant_id=1, ai_enabled=True))
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="运营群", auth_status="已授权运营", can_send=True, active_window="00:00-01:00"))
        session.add_all(
            [
                TgAccount(id=11, tenant_id=1, display_name="账号A", phone_masked="+861***0011", status="在线", session_ciphertext="session-a"),
                TgAccount(id=12, tenant_id=1, display_name="账号B", phone_masked="+861***0012", status="在线", session_ciphertext="session-b"),
            ]
        )
        session.add_all(
            [
                TgGroupAccount(tenant_id=1, group_id=7, account_id=11, can_send=True),
                TgGroupAccount(tenant_id=1, group_id=7, account_id=12, can_send=True),
                TgAccountOnlineState(tenant_id=1, account_id=11, desired_online=True, online_status="online", stale_after_at=now_value + timedelta(minutes=5)),
                TgAccountOnlineState(tenant_id=1, account_id=12, desired_online=True, online_status="online", stale_after_at=now_value + timedelta(minutes=5)),
                AiAccountVoiceProfile(tenant_id=1, account_id=11, version=1, status="active", quality_status="active", short_prompt_summary="青年短句，偶尔表情"),
                AiAccountVoiceProfile(tenant_id=1, account_id=12, version=1, status="active", quality_status="active", short_prompt_summary="中年短句，轻吐槽"),
            ]
        )
        task = Task(
            id="task-emoji-fallback",
            tenant_id=1,
            name="表情兜底",
            type="group_ai_chat",
            status="running",
            account_config={"selection_mode": "all", "max_concurrent": 10, "cooldown_per_account_minutes": 0},
            pacing_config={"max_actions_per_hour": 120},
            type_config={
                "target_group_id": 7,
                "messages_per_round_mode": "manual",
                "messages_per_round": 2,
                "reply_min_per_round": 0,
                "silent_mode_enabled": False,
                "fact_anchor_required": False,
                "low_confidence_silence_enabled": False,
                "account_coverage_mode": "all_accounts_daily",
            },
            stats={},
        )
        session.add(task)
        _add_ready_daily_coverage(session, task, [11, 12], coverage_date=now_value.date())
        session.add(
            Action(
                id="previous-ai-photo",
                tenant_id=1,
                task_id=task.id,
                task_type="group_ai_chat",
                action_type="send_message",
                account_id=11,
                status="success",
                scheduled_at=now_value - timedelta(minutes=1),
                executed_at=now_value - timedelta(minutes=1),
                payload={"message_text": "昨天照片准"},
            )
        )
        session.add(
            AiGroupMessageMemory(
                id="recent-emoji-memory",
                tenant_id=1,
                group_id=7,
                task_id="another-task",
                account_id=99,
                raw_text="👍",
                normalized_text="👍",
                text_fingerprint="recent-emoji",
                semantic_cluster="",
                status="success",
                planned_at=now_value - timedelta(minutes=2),
                sent_at=now_value - timedelta(minutes=2),
            )
        )
        session.add(
            GroupContextMessage(
                tenant_id=1,
                group_id=7,
                listener_account_id=11,
                sender_peer_id="9001",
                sender_name="真人A",
                content="昨天照片准，今天别一直重复这句",
                message_type="text",
                remote_message_id="7001",
                sent_at=now_value,
            )
        )
        session.commit()

        assert group_ai_chat.build_plan(session, task) == 2
        actions = list(
            session.scalars(
                select(Action)
                .where(Action.task_id == task.id, Action.action_type == "send_message", Action.status == "pending")
                .order_by(Action.scheduled_at, Action.created_at)
            )
        )
        memories = list(session.scalars(select(AiGroupMessageMemory).where(AiGroupMessageMemory.task_id == task.id).order_by(AiGroupMessageMemory.planned_at, AiGroupMessageMemory.id)))

    assert len(actions) == 2
    assert {action.payload["ai_generation_status"] for action in actions} == {"pending"}
    assert {action.payload["message_text"] for action in actions} == {""}
    assert {action.payload["quality_fallback"] for action in actions} == {""}
    assert memories == []


@pytest.mark.no_postgres
def test_group_ai_all_account_coverage_defers_voice_profile_candidates(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = datetime(2026, 7, 13, 23, 30)
    monkeypatch.setattr(group_ai_chat, "_now", lambda: now_value)
    monkeypatch.setattr(group_ai_chat.random, "sample", lambda pool, k: list(pool)[:k])
    _forbid_planner_ai_generation(monkeypatch)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="运营群", auth_status="已授权运营", can_send=True))
        for account_id in [11, 12]:
            session.add(TgAccount(id=account_id, tenant_id=1, display_name=f"账号{account_id}", phone_masked=str(account_id), status="在线", session_ciphertext=f"session-{account_id}"))
            session.add(TgGroupAccount(tenant_id=1, group_id=7, account_id=account_id, can_send=True))
            session.add(TgAccountOnlineState(tenant_id=1, account_id=account_id, desired_online=True, online_status="online", stale_after_at=now_value + timedelta(minutes=5)))
            session.add(
                AiAccountVoiceProfile(
                    tenant_id=1,
                    account_id=account_id,
                    version=1,
                    status="active",
                    short_prompt_summary="青年短句，少表情，不用表情",
                    emoji_policy="不用表情",
                )
            )
        task = Task(
            id="task-emoji-fallback-voice",
            tenant_id=1,
            name="表情兜底不被面具误杀",
            type="group_ai_chat",
            status="running",
            account_config={"selection_mode": "all", "max_concurrent": 10, "cooldown_per_account_minutes": 0},
            pacing_config={"max_actions_per_hour": 120},
            type_config={
                "target_group_id": 7,
                "messages_per_round_mode": "manual",
                "messages_per_round": 2,
                "reply_min_per_round": 0,
                "silent_mode_enabled": False,
                "fact_anchor_required": False,
                "low_confidence_silence_enabled": False,
                "account_coverage_mode": "all_accounts_daily",
            },
            stats={},
        )
        session.add(task)
        _add_ready_daily_coverage(session, task, [11, 12], coverage_date=now_value.date())
        session.add(
            Action(
                id="previous-ai-photo",
                tenant_id=1,
                task_id=task.id,
                task_type="group_ai_chat",
                action_type="send_message",
                account_id=11,
                status="success",
                scheduled_at=now_value - timedelta(minutes=1),
                executed_at=now_value - timedelta(minutes=1),
                payload={"message_text": "昨天照片准"},
            )
        )
        session.add(
            GroupContextMessage(
                tenant_id=1,
                group_id=7,
                listener_account_id=11,
                sender_peer_id="9001",
                sender_name="真人A",
                content="昨天照片准，今天别一直重复这句",
                message_type="text",
                remote_message_id="7001",
                sent_at=now_value,
            )
        )
        session.commit()

        created = group_ai_chat.build_plan(session, task)
        actions = list(session.scalars(select(Action).where(Action.task_id == task.id, Action.action_type == "send_message", Action.status == "pending").order_by(Action.created_at)))
        refreshed = session.get(Task, task.id)

    assert created == 2
    assert len(actions) == 2
    assert {action.payload["ai_generation_status"] for action in actions} == {"pending"}
    assert {action.payload["message_text"] for action in actions} == {""}
    assert {action.payload["account_voice_profile_summary"] for action in actions} == {"青年短句，少表情，不用表情"}
    assert refreshed.last_error == ""


def test_retry_failed_only_requeues_unknown_after_send_actions():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(Task(id="task-retry", tenant_id=1, name="retry", type="target_admission_retry", status="running"))
        session.add_all(
            [
                Action(id="action-success", tenant_id=1, task_id="task-retry", task_type="target_admission_retry", action_type="ensure_target_membership", status="success", scheduled_at=now_value),
                Action(id="action-unknown", tenant_id=1, task_id="task-retry", task_type="target_admission_retry", action_type="ensure_target_membership", status="unknown_after_send", scheduled_at=now_value, result={"error_code": "unknown_after_send"}),
                Action(id="action-failed", tenant_id=1, task_id="task-retry", task_type="target_admission_retry", action_type="ensure_target_membership", status="failed", scheduled_at=now_value, result={"error_code": "failed"}),
                Action(id="action-membership-denied", tenant_id=1, task_id="task-retry", task_type="target_admission_retry", action_type="ensure_target_membership", status="skipped", scheduled_at=now_value, result={"error_code": "membership_permission_denied", "membership_status": "permission_denied"}),
                Action(id="action-skipped", tenant_id=1, task_id="task-retry", task_type="target_admission_retry", action_type="ensure_target_membership", status="skipped", scheduled_at=now_value, result={"error_code": "already_joined"}),
            ]
        )
        session.commit()

        retry_task(session, 1, "task-retry", TaskRetryRequest(failed_only=True), "tester")

        assert session.get(Action, "action-success").status == "success"
        assert session.get(Action, "action-unknown").status == "pending"
        assert session.get(Action, "action-failed").status == "pending"
        assert session.get(Action, "action-membership-denied").status == "pending"
        assert session.get(Action, "action-skipped").status == "skipped"


def test_planner_backlog_ignores_expired_hard_hourly_pending_actions():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()
    expired_bucket = (now_value - timedelta(hours=2)).replace(minute=0, second=0, microsecond=0)
    current_bucket = now_value.replace(minute=0, second=0, microsecond=0)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        task = Task(id="task-hard-backlog", tenant_id=1, name="hard", type="group_ai_chat", status="running", type_config={"hard_hourly_target_enabled": True, "hourly_min_messages": 300})
        session.add(task)
        session.add_all(
            [
                Action(id="old-hard", tenant_id=1, task_id=task.id, task_type=task.type, action_type="send_message", status="pending", scheduled_at=expired_bucket, payload={"hard_hourly_target": True, "hard_hourly_bucket": expired_bucket.isoformat()}),
                Action(id="current-hard", tenant_id=1, task_id=task.id, task_type=task.type, action_type="send_message", status="pending", scheduled_at=now_value, payload={"hard_hourly_target": True, "hard_hourly_bucket": current_bucket.isoformat()}),
            ]
        )
        session.commit()

        snapshot = planner_backlog_snapshot(session, task)

    assert snapshot["task_pending"] == 1
    assert snapshot["global_pending"] == 1


@pytest.mark.no_postgres
def test_planner_backlog_does_not_materialize_open_actions():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()
    expired_bucket = (now_value - timedelta(hours=2)).replace(minute=0, second=0, microsecond=0)
    with Session(engine) as session:
        task = Task(id="task-backlog-bounded", tenant_id=1, name="bounded", type="group_ai_chat", status="running")
        other_task = Task(id="task-backlog-other", tenant_id=1, name="other", type="group_relay", status="running")
        session.add_all([Tenant(id=1, name="default"), task, other_task])
        session.add_all(
            [
                Action(id="expired-hard", tenant_id=1, task_id=task.id, task_type=task.type, action_type="send_message", status="pending", scheduled_at=expired_bucket, payload={"hard_hourly_target": True, "hard_hourly_bucket": expired_bucket.isoformat()}),
                Action(id="current-task", tenant_id=1, task_id=task.id, task_type=task.type, action_type="send_message", status="pending", scheduled_at=now_value, payload={}),
                Action(id="current-other", tenant_id=1, task_id=other_task.id, task_type=other_task.type, action_type="send_message", status="pending", scheduled_at=now_value, payload={}),
            ]
        )
        session.commit()
        session.expunge_all()
        task = session.get(Task, "task-backlog-bounded")
        loaded_actions = 0

        def capture_loaded_action(_session, instance):
            nonlocal loaded_actions
            loaded_actions += int(isinstance(instance, Action))

        event.listen(session, "loaded_as_persistent", capture_loaded_action)
        snapshot = planner_backlog_snapshot(session, task)

    assert snapshot["global_pending"] == 2
    assert snapshot["task_pending"] == 1
    assert loaded_actions == 0


@pytest.mark.no_postgres
def test_available_accounts_by_capacity_uses_bounded_queries():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    scheduled_at = datetime(2026, 7, 13, 12, 30)
    with Session(engine) as session:
        session.add(Tenant(id=1, name="default"))
        session.add(
            SchedulingSetting(
                tenant_id=1,
                default_account_cooldown_seconds=120,
                default_account_hour_limit=10,
                default_account_day_limit=50,
            )
        )
        session.commit()
        accounts = [TgAccount(id=account_id, tenant_id=1) for account_id in range(101, 681)]
        select_count = 0

        def count_selects(_conn, _cursor, statement, _parameters, _context, _executemany):
            nonlocal select_count
            select_count += int(statement.lstrip().upper().startswith("SELECT"))

        event.listen(engine, "before_cursor_execute", count_selects)
        cache = AccountCapacityCache()
        available = available_accounts_by_capacity(
            session,
            tenant_id=1,
            accounts=accounts,
            scheduled_at=scheduled_at,
            cache=cache,
        )
        adjacent_available = available_accounts_by_capacity(
            session,
            tenant_id=1,
            accounts=accounts,
            scheduled_at=scheduled_at + timedelta(seconds=10),
            cache=cache,
        )

    assert len(available) == 580
    assert len(adjacent_available) == 580
    assert select_count <= 3


@pytest.mark.no_postgres
def test_bulk_capacity_cache_preserves_hour_limit_decisions():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    scheduled_at = datetime(2026, 7, 13, 12, 30)
    accounts = [TgAccount(id=101, tenant_id=1), TgAccount(id=102, tenant_id=1)]
    with Session(engine) as session:
        session.add(Tenant(id=1, name="default"))
        session.add(SchedulingSetting(tenant_id=1, default_account_hour_limit=1))
        session.add(
            Action(
                id="occupied-account-101",
                tenant_id=1,
                task_id="capacity-task",
                task_type="group_ai_chat",
                action_type="send_message",
                account_id=101,
                status="success",
                scheduled_at=scheduled_at - timedelta(minutes=5),
                executed_at=scheduled_at - timedelta(minutes=5),
            )
        )
        session.commit()

        uncached = available_accounts_by_capacity(
            session,
            tenant_id=1,
            accounts=accounts,
            scheduled_at=scheduled_at,
        )
        cached = available_accounts_by_capacity(
            session,
            tenant_id=1,
            accounts=accounts,
            scheduled_at=scheduled_at,
            cache=AccountCapacityCache(),
        )

    assert [account.id for account in uncached] == [102]
    assert [account.id for account in cached] == [102]


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


def test_runtime_cleanup_batches_details_and_accumulates_totals():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    today = date(2026, 5, 15)
    old_at = datetime(2026, 5, 9, 10, 0, 0)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(Task(id="task-batch-clean", tenant_id=1, name="clean", type="group_relay", status="running"))
        session.add_all(
            Action(
                id=f"old-{index}", tenant_id=1, task_id="task-batch-clean", task_type="group_relay",
                action_type="send_message", status="success", scheduled_at=old_at, executed_at=old_at,
            )
            for index in range(3)
        )
        session.commit()

        assert cleanup_runtime_details(session, retention_days=5, today=today, batch_size=2) == 2
        session.commit()
        assert session.query(Action).count() == 1
        assert cleanup_runtime_details(session, retention_days=5, today=today, batch_size=2) == 1
        session.commit()

        total = session.query(DailyRuntimeStat).filter_by(
            stat_date=old_at.date(), dimension_type="global", dimension_id="all", metric_name="total",
        ).one()
        assert total.metric_value == 3
        assert session.query(RuntimeCleanupAudit).count() == 2

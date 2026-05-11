from datetime import datetime
from types import SimpleNamespace

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

import app.services.archives as archive_service
from app.config import Settings
from app.database import Base
from app.models import Action, AiUsageLedger, AuditLog, ChannelMessage, GroupArchive, GroupContextMessage, MessageFingerprint, MessageTask, OperationTarget, ReviewQueue, RuleSet, RuleSetVersion, Task, Tenant, TgAccount, TgGroup, TgGroupAccount
from app.schemas import ArchiveCreate, MessageSendTaskCreate
from app.services.audit import filter_audit_logs
from app.services.archives import create_archive
from app.services.messages import create_message_send_task, validate_group_task_policy
from app.services.operations import filter_operation_targets
from app.services.task_center.executors.group_ai_chat import ai_cycle_mode
from app.services.operations_center import listener_summary, operation_metrics_summary
from app.services.reports import build_overview
from app.services.task_center.executors.group_relay import apply_transform_rules, build_plan as build_group_relay_plan, resolve_relay_target_ids
from app.services.group_listeners import process_group_listener
from app.services.task_center.listener_runtime import reset_listener_runtime_cache, should_collect_listener
from app.services.task_center.fingerprints import content_fingerprint
from app.services.task_center.policies import validate_group_send_policy
from app.services.task_center.service import _channel_subtask_status, delete_task, reset_task, stop_task


def test_listener_runtime_deduplicates_same_object_within_window():
    reset_listener_runtime_cache()

    assert should_collect_listener("group", 1001, window_seconds=30) is True
    assert should_collect_listener("group", 1001, window_seconds=30) is False
    assert should_collect_listener("group", 1002, window_seconds=30) is True
    assert should_collect_listener("channel", 1001, window_seconds=30) is True

    reset_listener_runtime_cache()
    assert should_collect_listener("group", 1001, window_seconds=30) is True


def test_legacy_campaign_routes_are_opt_in_outside_test(monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://tg_yunying:tg_yunying@127.0.0.1:5432/tg_yunying")
    monkeypatch.delenv("ENABLE_LEGACY_CAMPAIGN_ROUTES", raising=False)
    assert Settings().enable_legacy_campaign_routes is False

    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("TEST_DATABASE_URL", "sqlite:///:memory:")
    assert Settings().enable_legacy_campaign_routes is True

    monkeypatch.setenv("ENABLE_LEGACY_CAMPAIGN_ROUTES", "0")
    assert Settings().enable_legacy_campaign_routes is False


def test_legacy_operation_task_routes_are_opt_in_outside_test(monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://tg_yunying:tg_yunying@127.0.0.1:5432/tg_yunying")
    monkeypatch.delenv("ENABLE_LEGACY_OPERATION_TASK_ROUTES", raising=False)
    assert Settings().enable_legacy_operation_task_routes is False

    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("TEST_DATABASE_URL", "sqlite:///:memory:")
    assert Settings().enable_legacy_operation_task_routes is True

    monkeypatch.setenv("ENABLE_LEGACY_OPERATION_TASK_ROUTES", "0")
    assert Settings().enable_legacy_operation_task_routes is False


def test_legacy_review_routes_are_opt_in_outside_test(monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://tg_yunying:tg_yunying@127.0.0.1:5432/tg_yunying")
    monkeypatch.delenv("ENABLE_LEGACY_REVIEW_ROUTES", raising=False)
    assert Settings().enable_legacy_review_routes is False

    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("TEST_DATABASE_URL", "sqlite:///:memory:")
    assert Settings().enable_legacy_review_routes is True

    monkeypatch.setenv("ENABLE_LEGACY_REVIEW_ROUTES", "0")
    assert Settings().enable_legacy_review_routes is False


def test_task_center_dispatch_ignores_legacy_review_queue_by_default(monkeypatch):
    from app.services.task_center import dispatcher

    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(Task(id="task-auto", tenant_id=1, name="自动发送", type="group_ai_chat", status="running"))
        session.add(Action(id="action-auto", tenant_id=1, task_id="task-auto", task_type="group_ai_chat", action_type="send_message", status="pending"))
        session.add(ReviewQueue(id="review-old", tenant_id=1, task_id="task-auto", action_id="action-auto", status="pending", content_preview="旧审核队列残留"))
        session.commit()

        monkeypatch.setattr(dispatcher, "get_settings", lambda: SimpleNamespace(enable_legacy_review_dispatch_gate=False))
        assert [action.id for action in dispatcher.due_actions(session)] == ["action-auto"]

        monkeypatch.setattr(dispatcher, "get_settings", lambda: SimpleNamespace(enable_legacy_review_dispatch_gate=True))
        assert dispatcher.due_actions(session) == []


def test_listener_summary_uses_task_subscriptions_events_and_backlog():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add_all(
            [
                OperationTarget(id=21, tenant_id=1, target_type="channel", tg_peer_id="-10021", title="频道", can_send=True, auth_status="已授权运营"),
                ChannelMessage(id=31, tenant_id=1, channel_target_id=21, message_id=1001, content_preview="频道消息", published_at=datetime(2026, 5, 11, 9, 0, 0)),
                TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="源群", auth_status="已授权运营", listener_enabled=True),
                TgGroupAccount(id=71, tenant_id=1, group_id=7, account_id=12, is_listener=True),
                GroupContextMessage(id=41, tenant_id=1, group_id=7, listener_account_id=12, sender_name="用户", content="源群事件", remote_message_id="m1", sent_at=datetime(2026, 5, 11, 10, 0, 0)),
                Task(id="task-channel", tenant_id=1, name="频道任务", type="channel_like", status="running", account_config={"account_ids": [11]}, type_config={"target_channel_id": 21}),
                Task(id="task-relay", tenant_id=1, name="转发任务", type="group_relay", status="running", type_config={"source_groups": [{"group_id": 7, "is_active": True}]}),
                Action(id="action-channel", tenant_id=1, task_id="task-channel", task_type="channel_like", action_type="like_message", status="pending"),
                Action(id="action-relay", tenant_id=1, task_id="task-relay", task_type="group_relay", action_type="send_message", status="executing"),
            ]
        )
        session.commit()

        summary = listener_summary(session, 1)

    rows = {item.key: item for item in summary.items}
    assert rows["channel:21"].subscriber_task_count == 1
    assert rows["channel:21"].listener_account_count == 1
    assert rows["channel:21"].event_backlog_count == 1
    assert rows["channel:21"].last_event_at == "2026-05-11T09:00:00"
    assert rows["group:7"].subscriber_task_count == 1
    assert rows["group:7"].listener_account_count == 1
    assert rows["group:7"].event_backlog_count == 1
    assert rows["group:7"].last_event_at == "2026-05-11T10:00:00"


def test_group_listener_context_collection_does_not_trigger_legacy_campaign_by_default(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="源群", auth_status="已授权运营", listener_enabled=True))
        session.commit()

        monkeypatch.setattr("app.services.group_listeners.get_settings", lambda: SimpleNamespace(enable_legacy_campaign_worker=False))
        monkeypatch.setattr("app.services.group_listeners.collect_group_context", lambda _session, _group: 1)

        def fail_auto_reply(*_args, **_kwargs):
            raise AssertionError("legacy Campaign auto-reply should be disabled by default")

        monkeypatch.setattr("app.services.group_listeners.trigger_listener_auto_reply", fail_auto_reply)

        assert process_group_listener(session, 7) == 1
        assert session.get(TgGroup, 7).listener_last_error == ""


def test_group_message_policy_uses_auto_validation_without_draft_gate():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        group = TgGroup(
            id=7,
            tenant_id=1,
            tg_peer_id="-1007",
            title="目标群",
            auth_status="已授权运营",
            can_send=True,
            require_review=True,
            daily_limit=120,
            group_cooldown_seconds=0,
            banned_words="",
        )
        task = MessageTask(
            id=70,
            tenant_id=1,
            group_id=7,
            content="自动校验通过的消息",
            target_type="group",
            idempotency_key="auto-validation-no-draft-gate",
        )
        session.add_all([group, task])
        session.commit()

        assert validate_group_task_policy(session, task, group) == (None, None)


def test_task_center_group_policy_uses_auto_validation_without_review_gate():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        group = TgGroup(
            id=7,
            tenant_id=1,
            tg_peer_id="-1007",
            title="目标群",
            auth_status="已授权运营",
            can_send=True,
            require_review=True,
            daily_limit=120,
            group_cooldown_seconds=0,
            banned_words="",
        )
        session.add(group)
        session.commit()

        assert validate_group_send_policy(
            session,
            tenant_id=1,
            group=group,
            content="自动校验通过的任务中心消息",
            review_approved=False,
        ) == (None, None)


def test_group_relay_transform_rules_are_applied_before_auto_send():
    content = "公告 @alice 访问 https://old.example/a 旧词保留"

    transformed = apply_transform_rules(
        content,
        {
            "remove_mentions": True,
            "remove_links": True,
            "replace_links": {"https://old.example/a": "https://new.example/b"},
            "keyword_replacements": {"旧词": "新词"},
            "prefix": "[转发] ",
            "suffix": " #已处理",
        },
    )

    assert transformed == "[转发] 公告  访问  新词保留 #已处理"

    assert apply_transform_rules(
        "访问 https://old.example/a 和 https://unknown.example",
        {"replace_links": {"https://old.example/a": "https://new.example/b", "*": "https://fallback.example"}},
    ) == "访问 https://new.example/b 和 https://fallback.example"


def test_group_relay_routing_rules_select_multiple_targets():
    config = {
        "target_group_id": 10,
        "target_group_ids": [10, 11],
        "routing": {
            "source_group_map": {"200": [12]},
            "keyword_routes": [
                {"keywords": ["公告"], "target_group_ids": [13, 14]},
                {"keyword": "活动", "target_group_ids": [15]},
            ],
            "routes": [
                {"source_group_ids": [200], "keywords": ["公告"], "target_group_ids": [16, 13]},
            ],
        },
    }

    assert resolve_relay_target_ids(config, 200, "今日公告") == [12, 16, 13, 14]
    assert resolve_relay_target_ids(config, 201, "普通消息") == [10, 11]


def test_group_relay_rule_account_strategy_controls_sender(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add_all(
            [
                TgAccount(id=101, tenant_id=1, display_name="账号A", phone_masked="101", status="在线", health_score=100),
                TgAccount(id=102, tenant_id=1, display_name="账号B", phone_masked="102", status="在线", health_score=90),
                TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="源群", auth_status="已授权运营", listener_context_limit=20),
                TgGroup(id=9, tenant_id=1, tg_peer_id="-1009", title="目标群", auth_status="已授权运营", listener_context_limit=20),
                TgGroupAccount(id=901, tenant_id=1, group_id=9, account_id=101, can_send=True),
                TgGroupAccount(id=902, tenant_id=1, group_id=9, account_id=102, can_send=True),
                GroupContextMessage(
                    id=41,
                    tenant_id=1,
                    group_id=7,
                    listener_account_id=101,
                    sender_peer_id="user-1",
                    sender_name="用户",
                    content="公告：今晚活动开始",
                    remote_message_id="src-1",
                    sent_at=datetime(2026, 5, 11, 10, 0, 0),
                ),
                RuleSet(id=31, tenant_id=1, name="固定账号规则", status="active", active_version_id=32),
                RuleSetVersion(
                    id=32,
                    tenant_id=1,
                    rule_set_id=31,
                    version=1,
                    status="published",
                    filters={"keyword_whitelist": ["公告"]},
                    transforms={},
                    routing={"target_group_ids": [9]},
                    account_strategy={"mode": "fixed", "account_id": 102},
                    retry_policy={},
                    rate_limits={},
                    created_by="tester",
                    published_by="tester",
                ),
                Task(
                    id="relay-strategy",
                    tenant_id=1,
                    name="规则账号策略",
                    type="group_relay",
                    status="running",
                    account_config={"selection_mode": "all", "max_concurrent": 5, "cooldown_per_account_minutes": 0},
                    pacing_config={"mode": "fixed", "interval_seconds_min": 0, "interval_seconds_max": 0, "jitter_percent": 0},
                    type_config={
                        "source_groups": [{"group_id": 7, "is_active": True}],
                        "target_group_id": 9,
                        "rule_set_version_id": 32,
                        "content_mode": "raw",
                        "dedup_window_minutes": 60,
                    },
                ),
            ]
        )
        session.commit()

        monkeypatch.setattr("app.services.task_center.executors.group_relay.should_collect_listener", lambda *_args, **_kwargs: False)

        assert build_group_relay_plan(session, session.get(Task, "relay-strategy")) == 1
        action = session.scalar(select(Action).where(Action.task_id == "relay-strategy"))
        account_id = action.account_id

    assert account_id == 102


def test_channel_subtask_status_prefers_capacity_and_progress():
    assert _channel_subtask_status({"target_count": 50, "completed_count": 38, "running_count": 4, "capacity_shortfall": 8}) == "容量不足"
    assert _channel_subtask_status({"target_count": 50, "completed_count": 50, "running_count": 0, "capacity_shortfall": 0}) == "已达标"
    assert _channel_subtask_status({"target_count": 50, "completed_count": 10, "failed_count": 2, "running_count": 0, "capacity_shortfall": 0}) == "有失败"


def test_ai_cycle_mode_applies_silent_window_and_daily_ramp():
    config = {
        "silent_mode_enabled": True,
        "silent_start": "23:00",
        "silent_end": "08:00",
        "ramp_up_minutes": 60,
        "ramp_start_ratio": 0.25,
    }

    assert ai_cycle_mode(config, datetime(2026, 5, 11, 9, 0), datetime(2026, 5, 11, 9, 15)) == ("启动期", 0.438)
    assert ai_cycle_mode(config, datetime(2026, 5, 11, 9, 0), datetime(2026, 5, 11, 23, 30)) == ("静默期", 1.0)


def test_operation_metrics_summary_uses_real_task_center_tables():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add_all(
            [
                TgAccount(tenant_id=1, display_name="在线号", phone_masked="+861***0001", status="在线", health_score=98),
                TgAccount(tenant_id=1, display_name="异常号", phone_masked="+861***0002", status="异常", health_score=40),
                OperationTarget(tenant_id=1, target_type="group", tg_peer_id="g1", title="目标群", can_send=True, auth_status="已授权运营"),
                OperationTarget(tenant_id=1, target_type="channel", tg_peer_id="c1", title="频道", can_send=False, auth_status="已授权运营"),
                Task(id="task-ai", tenant_id=1, name="AI 活跃", type="group_ai_chat", status="running"),
                Task(id="task-relay", tenant_id=1, name="转发监听", type="group_relay", status="running"),
                Action(id="a1", tenant_id=1, task_id="task-ai", task_type="group_ai_chat", action_type="send_message", status="success", executed_at=datetime(2026, 5, 11, 1, 0, 0)),
                Action(id="a2", tenant_id=1, task_id="task-relay", task_type="group_relay", action_type="send_message", status="failed"),
                Action(id="a3", tenant_id=1, task_id="task-relay", task_type="group_relay", action_type="like_message", status="success"),
                GroupArchive(tenant_id=1, group_id=1, title="归档", message_count=12, member_count=3),
                AiUsageLedger(tenant_id=1, user_id=1, total_tokens=123, total_cost=0.45),
            ]
        )
        session.commit()

        summary = operation_metrics_summary(session, 1)

    assert next(item.value for item in summary.accounts if item.key == "accounts.total") == 2
    assert next(item.value for item in summary.targets if item.key == "targets.total") == 2
    assert next(item.value for item in summary.ai_activity if item.key == "ai_activity.sent") == 1
    assert next(item.value for item in summary.relay if item.key == "relay.failed") == 1
    assert next(item.value for item in summary.archives if item.key == "archives.messages") == 12
    assert next(item.value for item in summary.ai_usage if item.key == "ai_usage.tokens") == 123


def test_overview_counts_new_task_center_tasks_not_legacy_campaigns():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(Task(id="task-overview", tenant_id=1, name="新版任务", type="group_ai_chat", status="running"))
        session.commit()

        overview = build_overview(session, 1)

    assert overview["totals"]["tasks"] == 1
    assert overview["totals"]["campaigns"] == 1


def test_operation_targets_expose_linked_group_capability_summary():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(OperationTarget(tenant_id=1, target_type="group", tg_peer_id="-1001", title="运营群", can_send=True, auth_status="已授权运营"))
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1001", title="运营群", auth_status="已授权运营", can_send=True, listener_enabled=True))
        session.add_all(
            [
                TgAccount(id=11, tenant_id=1, display_name="发送号", phone_masked="+861***0011", status="在线"),
                TgAccount(id=12, tenant_id=1, display_name="监听号", phone_masked="+861***0012", status="在线"),
                TgGroupAccount(tenant_id=1, group_id=7, account_id=11, can_send=True),
                TgGroupAccount(tenant_id=1, group_id=7, account_id=12, can_send=False, is_listener=True),
            ]
        )
        session.commit()

        targets = filter_operation_targets(session, 1, "group")

    assert targets[0]["linked_group_id"] == 7
    assert targets[0]["available_send_account_count"] == 1
    assert targets[0]["listener_account_count"] == 1
    assert targets[0]["can_listen"] is True


def test_message_send_group_operation_target_checks_account_permission():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add_all(
            [
                TgAccount(id=11, tenant_id=1, display_name="可发号", phone_masked="+861***0011", status="在线"),
                TgAccount(id=12, tenant_id=1, display_name="不可发号", phone_masked="+861***0012", status="在线"),
                TgGroup(id=7, tenant_id=1, tg_peer_id="-1001", title="运营群", auth_status="已授权运营", can_send=True),
                TgGroupAccount(tenant_id=1, group_id=7, account_id=11, can_send=True),
                OperationTarget(id=21, tenant_id=1, target_type="group", tg_peer_id="-1001", title="运营群", can_send=True, auth_status="已授权运营"),
            ]
        )
        session.commit()

        created = create_message_send_task(
            session,
            MessageSendTaskCreate(account_id=11, target_type="group", operation_target_id=21, content="hello"),
            "tester",
            1,
        )
        assert created.group_id == 7
        assert created.target_peer_id == "-1001"

        try:
            create_message_send_task(
                session,
                MessageSendTaskCreate(account_id=12, target_type="group", operation_target_id=21, content="hello"),
                "tester",
                1,
            )
        except ValueError as exc:
            assert "不可向此运营目标发送" in str(exc)
        else:
            raise AssertionError("expected account permission validation to fail")


def test_archive_can_be_created_from_operation_target(monkeypatch):
    monkeypatch.setattr(archive_service, "get_settings", lambda: SimpleNamespace(tg_gateway_mode="telethon"))
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add_all(
            [
                TgAccount(id=11, tenant_id=1, display_name="归档号", phone_masked="+861***0011", status="在线"),
                TgGroup(id=7, tenant_id=1, tg_peer_id="-1001", title="归档群", auth_status="已授权运营", can_send=True),
                TgGroupAccount(tenant_id=1, group_id=7, account_id=11, can_send=True),
                OperationTarget(id=21, tenant_id=1, target_type="group", tg_peer_id="-1001", title="归档群", can_send=True, auth_status="已授权运营"),
            ]
        )
        session.commit()

        archive = create_archive(session, ArchiveCreate(operation_target_id=21, title="归档群内容归档"), "tester")

    assert archive.group_id == 7
    assert archive.title == "归档群内容归档"


def test_task_stop_and_delete_keep_distinct_terminal_statuses():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add_all(
            [
                Task(id="task-stop", tenant_id=1, name="停止任务", type="group_relay", status="running"),
                Task(id="task-delete", tenant_id=1, name="删除任务", type="group_relay", status="running"),
                Action(id="action-stop", tenant_id=1, task_id="task-stop", task_type="group_relay", action_type="send_message", status="pending"),
                Action(id="action-delete", tenant_id=1, task_id="task-delete", task_type="group_relay", action_type="send_message", status="executing"),
            ]
        )
        session.commit()

        stopped = stop_task(session, 1, "task-stop", "tester")
        delete_task(session, 1, "task-delete", "tester")
        deleted = session.get(Task, "task-delete")
        stop_action = session.get(Action, "action-stop")
        delete_action = session.get(Action, "action-delete")
        stopped_status = stopped.status
        deleted_status = deleted.status
        deleted_at = deleted.deleted_at
        stop_action_status = stop_action.status
        stop_action_error_code = stop_action.result["error_code"]
        delete_action_status = delete_action.status
        delete_action_error_code = delete_action.result["error_code"]

    assert stopped_status == "stopped"
    assert stop_action_status == "skipped"
    assert stop_action_error_code == "task_stopped"
    assert deleted_status == "deleted"
    assert deleted_at is not None
    assert delete_action_status == "skipped"
    assert delete_action_error_code == "task_deleted"


def test_worker_keeps_legacy_campaign_and_operation_drains_opt_in(monkeypatch):
    from app import worker

    monkeypatch.setattr(worker, "get_settings", lambda: SimpleNamespace(enable_legacy_campaign_worker=False, enable_legacy_operation_task_worker=False))
    monkeypatch.setattr(worker, "drain_profile_sync_records", lambda *args, **kwargs: 0)
    monkeypatch.setattr(worker, "drain_account_sync_records", lambda *args, **kwargs: 0)
    monkeypatch.setattr(worker, "drain_group_listeners", lambda *args, **kwargs: 0)
    monkeypatch.setattr(worker, "drain_task_center", lambda *args, **kwargs: 0)
    monkeypatch.setattr(worker, "drain_archives", lambda *args, **kwargs: 0)
    monkeypatch.setattr(worker, "drain_continuous_campaigns", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("legacy campaign worker must be opt-in")))
    monkeypatch.setattr(worker, "drain_operation_tasks", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("legacy operation worker must be opt-in")))
    monkeypatch.setattr(worker, "get_task_queue", lambda: SimpleNamespace(size=lambda: 0, dequeue=lambda: None))

    assert worker.drain_once(10) == 0


def test_task_center_pre_send_validation_records_auto_check_metadata(monkeypatch):
    from app.services.task_center import dispatcher

    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *args, **kwargs: object())
    monkeypatch.setattr(dispatcher.gateway, "send_message", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("blocked content must not call TG")))

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add_all(
            [
                TgAccount(id=11, tenant_id=1, display_name="发送号", phone_masked="+861***0011", status="在线"),
                TgGroup(id=7, tenant_id=1, tg_peer_id="-1001", title="运营群", auth_status="已授权运营", can_send=True, banned_words="敏感词"),
                TgGroupAccount(tenant_id=1, group_id=7, account_id=11, can_send=True),
                Task(id="task-auto-check", tenant_id=1, name="自动校验", type="group_ai_chat", status="running"),
                Action(
                    id="action-auto-check",
                    tenant_id=1,
                    task_id="task-auto-check",
                    task_type="group_ai_chat",
                    action_type="send_message",
                    account_id=11,
                    status="pending",
                    payload={"group_id": 7, "message_text": "这里有敏感词", "review_approved": True},
                    result={},
                ),
            ]
        )
        session.commit()

        action = session.get(Action, "action-auto-check")
        assert dispatcher.dispatch_action(session, action) is True

        assert action.status == "failed"
        assert action.result["auto_check"] == "拦截"
        assert action.result["validation_stage"] == "content_policy"
        assert "敏感词" in action.result["error_message"]


def test_task_reset_preserves_finished_evidence_and_dedup_fingerprints():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add_all(
            [
                Task(id="task-reset", tenant_id=1, name="重置任务", type="group_relay", status="running"),
                Action(id="action-success", tenant_id=1, task_id="task-reset", task_type="group_relay", action_type="send_message", status="success", result={"success": True}),
                Action(id="action-failed", tenant_id=1, task_id="task-reset", task_type="group_relay", action_type="send_message", status="failed", result={"success": False}),
                Action(
                    id="action-pending",
                    tenant_id=1,
                    task_id="task-reset",
                    task_type="group_relay",
                    action_type="send_message",
                    status="pending",
                    payload={"source_group_id": 7, "group_id": 9, "original_text": "pending source"},
                ),
                MessageFingerprint(tenant_id=1, source_group_id="task-reset:relay:7", fingerprint="abc", original_text="done"),
                MessageFingerprint(tenant_id=1, source_group_id="task-reset:relay:7:target:9", fingerprint=content_fingerprint("done source"), original_text="done source"),
                MessageFingerprint(tenant_id=1, source_group_id="task-reset:relay:7:target:9", fingerprint=content_fingerprint("pending source"), original_text="pending source"),
            ]
        )
        session.commit()

        reset = reset_task(session, 1, "task-reset", "tester")
        reset_status = reset.status
        actions = {action.id: action.status for action in session.scalars(select(Action).where(Action.task_id == "task-reset"))}
        fingerprints = session.scalars(select(MessageFingerprint).where(MessageFingerprint.source_group_id == "task-reset:relay:7")).all()
        relay_fingerprints = session.scalars(select(MessageFingerprint).where(MessageFingerprint.source_group_id == "task-reset:relay:7:target:9")).all()

    assert reset_status == "running"
    assert actions == {"action-success": "success", "action-failed": "failed"}
    assert len(fingerprints) == 1
    assert [item.original_text for item in relay_fingerprints] == ["done source"]


def test_audit_logs_support_operational_filters():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add_all(
            [
                AuditLog(tenant_id=1, actor="admin", action="启动任务中心任务", target_type="task", target_id="task-1", detail="group_relay success"),
                AuditLog(tenant_id=1, actor="worker", action="执行消息发送失败", target_type="message_task", target_id="99", detail="task-1 账号不可用"),
                AuditLog(tenant_id=1, actor="admin", action="同步TG账号", target_type="tg_account", target_id="42", detail="contacts=3"),
                AuditLog(tenant_id=2, actor="admin", action="启动任务中心任务", target_type="task", target_id="task-2", detail="other tenant"),
            ]
        )
        session.commit()

        assert [item.target_id for item in filter_audit_logs(session, 1, task_id="task-1")] == ["99", "task-1"]
        assert [item.target_id for item in filter_audit_logs(session, 1, account_id="42")] == ["42"]
        assert [item.target_id for item in filter_audit_logs(session, 1, status="failed")] == ["99"]
        assert [item.target_id for item in filter_audit_logs(session, 1, keyword="group_relay")] == ["task-1"]

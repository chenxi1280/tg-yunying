import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session, sessionmaker

import app.services.archives as archive_service
from app.ai_gateway import AiDraftCandidate, AiGenerationResult, AiUsage
from app.config import Settings
from app.database import Base
from app.integrations.telegram import OperationResult, SendResult, _resolve_telethon_target, _telethon_send_target
from app.integrations.telegram.gateway import TelethonTelegramGateway
from app.models import AccountStatus, Action, AiProvider, AiUsageLedger, AuditLog, ChannelMessage, ChannelMessageComment, ContentKeywordRule, FailureType, GroupArchive, GroupContextMessage, ListenerSourceState, MessageFingerprint, MessageTask, MessageTaskAttempt, OperationIssue, OperationIssueAccount, OperationIssueSource, OperationTarget, PromptTemplate, ReviewQueue, RuleSet, RuleSetVersion, SchedulingSetting, TargetRuntimeSummary, Task, TaskRuntimeSummary, TaskStatus, Tenant, TenantAiSetting, TgAccount, TgAccountSyncRecord, TgGroup, TgGroupAccount, VerificationTask, WorkerHeartbeat
from app.schemas import ArchiveCreate, ChannelCommentTaskCreate, ChannelLikeTaskCreate, ChannelViewTaskCreate, GroupAIChatTaskCreate, GroupRelayTaskCreate, MaterialCreate, MaterialUpdate, MessageSendTaskCreate, OperationTargetAccountUpdate, OperationTargetAdmissionRetryRequest, OperationTargetUpdate, PromptTemplateCreate, PromptTemplateUpdate, SchedulingSettingUpdate, TaskPrecheckRequest, TaskSettingsUpdate, TaskSourceFilterOverrideRequest
from app.schemas.operations_center import RuleSetVersionCreate
from app.schemas.risk_control import RiskControlGlobalPolicyUpdate
from app.security import encrypt_secret
import app.services.accounts as account_service
from app.services._common import _now
from app.services.audit import audit_logs_csv, filter_audit_logs
from app.services.archives import create_archive
from app.services.ai_config import create_material, create_prompt_template, get_scheduling_setting, update_material, update_prompt_template, update_scheduling_setting
from app.services.risk_control import update_global_policy
from app.services.account_capacity import AccountCapacityCache, AccountCapacityReservation, account_capacity_decision
from app.services.messages import create_message_send_task, dispatch_task, filter_tasks, retry_task, validate_group_task_policy
from app.services.operations import filter_operation_targets, operation_target_detail, retry_operation_target_admission, sync_all_operation_targets, update_operation_target, update_operation_target_account_policy
from app.services.verification import resolve_group_restriction_batch
from app.services.task_center.executors.group_ai_chat import _choose_turn_account, _topic_relevant_context_rows, ai_cycle_mode, build_plan as build_group_ai_chat_plan
from app.services.task_center.ai_generator import _humanize_group_chat_punctuation
from app.services.operations_center import _is_stale_heartbeat, listener_summary, list_listener_errors, list_listener_events, list_rule_sets, operation_metrics_summary, relay_attribution_csv, relay_attribution_report, reset_listener_watermark, rule_center_summary, switch_listener_account, test_rules as preview_rules, update_rule_set_config
from app.services.reports import _hourly_activity_24h, build_overview
from app.services.task_center.executors.group_relay import apply_transform_rules, build_plan as build_group_relay_plan, passes_relay_filters, relay_source_filter_reason, resolve_relay_target_ids
from app.services.task_center.executors.channel_like import build_plan as build_channel_like_plan
from app.services.task_center.pacing import schedule_times
from app.services.group_listeners import process_group_listener
from app.services.task_center.listener_runtime import drain_listener_runtime, reset_listener_runtime_cache, should_collect_listener
from app.services.task_center.fingerprints import content_fingerprint
from app.services.task_center.policies import validate_group_send_policy
from app.services.task_center.service import _action_payload, _channel_subtask_status, _planning_backlog_blocked, _recover_stale_executing_actions, _retry_failed_actions, add_task_source_filter_override, create_group_ai_chat_task, create_group_relay_task, delete_task, drain_task_center, get_task_detail, list_tasks, precheck_task_creation, reset_task, stop_task, update_task_settings
from app.services.task_center.executors.channel_comment import build_plan as build_channel_comment_plan
from app.services.task_center.payloads import ViewMessagePayload, create_view_action
from app.services.task_center.stats import planner_backlog_snapshot, refresh_task_stats
from app.services.runtime_summary import get_operation_issue_detail, refresh_task_summary, upsert_operation_issue
from app.timezone import BEIJING_TZ, beijing_day_bounds


def test_listener_runtime_deduplicates_same_object_within_window():
    reset_listener_runtime_cache()

    assert should_collect_listener("group", 1001, window_seconds=30) is True
    assert should_collect_listener("group", 1001, window_seconds=30) is False
    assert should_collect_listener("group", 1002, window_seconds=30) is True
    assert should_collect_listener("channel", 1001, window_seconds=30) is True

    reset_listener_runtime_cache()
    assert should_collect_listener("group", 1001, window_seconds=30) is True


def test_create_view_action_returns_existing_dedupe_action():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        scheduled_at = datetime(2026, 5, 31, 20, 0)
        task = Task(id="task-view-dedupe", tenant_id=1, name="频道浏览", type="channel_view", status="running")
        payload = ViewMessagePayload(
            channel_id="jdkejshe",
            channel_target_id=6,
            channel_message_id=66,
            message_id=66,
            execution_date="2026-05-31",
        )
        session.add_all([Tenant(id=1, name="默认运营空间"), task])
        session.commit()

        first = create_view_action(session, task, 110, scheduled_at, payload)
        second = create_view_action(session, task, 110, scheduled_at, payload)

        assert second.id == first.id
        assert session.query(Action).filter_by(task_id=task.id, action_type="view_message").count() == 1


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


def test_business_clock_and_day_bounds_use_beijing_time():
    before = datetime.now(BEIJING_TZ).replace(tzinfo=None)
    current = _now()
    after = datetime.now(BEIJING_TZ).replace(tzinfo=None)
    day_start, day_end = beijing_day_bounds(current)

    assert before <= current <= after
    assert day_start == current.replace(hour=0, minute=0, second=0, microsecond=0)
    assert day_end == day_start + timedelta(days=1)


def test_task_center_list_does_not_load_channel_message_detail():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    statements: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def _capture_sql(_conn, _cursor, statement, _parameters, _context, _executemany):  # noqa: ANN001
        statements.append(statement)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(OperationTarget(id=101, tenant_id=1, target_type="channel", tg_peer_id="-100101", title="频道", username="chan"))
        session.add(
            Task(
                id="task-list-fast",
                tenant_id=1,
                name="列表速度",
                type="channel_like",
                status="running",
                type_config={"target_channel_id": 101, "message_ids": []},
                stats={"success_count": 1},
            )
        )
        session.add(ChannelMessage(tenant_id=1, channel_target_id=101, message_id=55, content_preview="不应进入列表搜索", message_url="https://t.me/chan/55"))
        session.commit()
        statements.clear()

        rows = list_tasks(session, 1)

    assert rows[0]["target_summary"] == "频道 @chan"
    assert "不应进入列表搜索" not in rows[0]["search_text"]
    assert not any("channel_messages" in statement.lower() for statement in statements)


def test_task_center_list_treats_all_filters_as_unfiltered():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add_all(
            [
                Task(id="task-filter-running", tenant_id=1, name="运行任务", type="group_ai_chat", status="running"),
                Task(id="task-filter-paused", tenant_id=1, name="暂停任务", type="channel_like", status="paused"),
            ]
        )
        session.commit()

        status_rows = list_tasks(session, 1, status="all")
        chinese_status_rows = list_tasks(session, 1, status="全部")
        type_rows = list_tasks(session, 1, task_type="all")

    assert {row["id"] for row in status_rows} == {"task-filter-running", "task-filter-paused"}
    assert {row["id"] for row in chinese_status_rows} == {"task-filter-running", "task-filter-paused"}
    assert {row["id"] for row in type_rows} == {"task-filter-running", "task-filter-paused"}


def test_task_center_action_payload_explains_group_permission_failures():
    action = Action(
        id="diagnose-group-permission",
        tenant_id=1,
        task_id="task-permission",
        task_type="group_ai_chat",
        action_type="send_message",
        account_id=8,
        status="failed",
        result={
            "success": False,
            "error_code": "未知错误",
            "error_message": "The channel specified is private and you lack permission to access it. Another reason may be that you were banned from it (caused by SendMessageRequest)",
            "validation_stage": "telegram_api",
        },
    )

    payload = _action_payload(action)

    assert payload["failure_diagnosis"]["category"] == "target_permission"
    assert payload["failure_diagnosis"]["scope"] == "account_target"
    assert "不是账号掉线" in payload["failure_diagnosis"]["operator_summary"]
    assert "目标群" in payload["failure_diagnosis"]["suggested_action"]


def test_task_center_action_payload_explains_comment_unavailable_failures():
    action = Action(
        id="diagnose-comment-unavailable",
        tenant_id=1,
        task_id="task-comment",
        task_type="channel_comment",
        action_type="post_comment",
        account_id=8,
        status="failed",
        result={
            "success": False,
            "error_code": FailureType.COMMENT_UNAVAILABLE.value,
            "error_message": "频道帖子无法解析到评论区，请确认消息ID属于频道帖子、频道已绑定讨论组，且执行账号可进入讨论组并评论",
            "validation_stage": "telegram_api",
        },
    )

    payload = _action_payload(action)

    assert payload["failure_diagnosis"]["category"] == "comment_unavailable"
    assert payload["failure_diagnosis"]["scope"] == "channel_message"
    assert "无法解析到评论区" in payload["failure_diagnosis"]["operator_summary"]
    assert "重新采集频道消息" in payload["failure_diagnosis"]["suggested_action"]


def test_message_task_list_treats_all_status_as_unfiltered():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add_all(
            [
                MessageTask(tenant_id=1, content="排队任务", status=TaskStatus.QUEUED.value, idempotency_key="all-status-queued"),
                MessageTask(tenant_id=1, content="失败任务", status=TaskStatus.FAILED.value, idempotency_key="all-status-failed"),
            ]
        )
        session.commit()

        rows = filter_tasks(session, 1, 1, 10, None, "all")
        chinese_rows = filter_tasks(session, 1, 1, 10, None, "全部")

    assert {row.content for row in rows} == {"排队任务", "失败任务"}
    assert {row.content for row in chinese_rows} == {"排队任务", "失败任务"}


def test_task_list_and_detail_expose_derived_runtime_stage():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(
            Task(
                id="task-paused-visible",
                tenant_id=1,
                name="暂停要明显",
                type="group_ai_chat",
                status="paused",
                next_run_at=None,
                last_error="账号受限后暂停",
                stats={"last_failure_policy": "pause_task"},
            )
        )
        session.add(
            Task(
                id="task-waiting-ai",
                tenant_id=1,
                name="等待 AI",
                type="group_ai_chat",
                status="running",
                next_run_at=now_value,
                last_error="AI 生成不可用，等待恢复后继续执行：The read operation timed out",
                stats={"ai_unavailable_reason": "The read operation timed out"},
            )
        )
        session.add(
            Task(
                id="task-startup-visible",
                tenant_id=1,
                name="启动中要明显",
                type="group_ai_chat",
                status="pending",
            )
        )
        session.add(
            Task(
                id="task-membership-and-ai",
                tenant_id=1,
                name="准入和 AI 同时可见",
                type="group_ai_chat",
                status="running",
                last_error="AI 生成不可用，等待恢复后继续执行：The read operation timed out",
                stats={
                    "membership_stage": "membership_partial",
                    "membership_need_join_count": 48,
                    "ai_unavailable_reason": "The read operation timed out",
                },
            )
        )
        session.commit()

        refresh_task_summary(session, session.get(Task, "task-waiting-ai"))
        rows = {row["id"]: row for row in list_tasks(session, 1)}
        paused_detail = get_task_detail(session, 1, "task-paused-visible")
        ai_summary = refresh_task_summary(session, session.get(Task, "task-waiting-ai"))

    assert rows["task-paused-visible"]["runtime_stage"]["stage_code"] == "paused"
    assert rows["task-paused-visible"]["runtime_stage"]["stage_label"] == "已暂停"
    assert rows["task-paused-visible"]["runtime_stage"]["severity"] == "danger"
    assert "不会继续规划或执行新动作" in rows["task-paused-visible"]["runtime_stage"]["reason"]
    assert paused_detail["task"]["runtime_stage"]["stage_code"] == "paused"
    assert rows["task-waiting-ai"]["runtime_stage"]["stage_code"] == "waiting_ai"
    assert rows["task-waiting-ai"]["runtime_stage"]["stage_label"] == "等待 AI"
    assert "The read operation timed out" in rows["task-waiting-ai"]["runtime_stage"]["reason"]
    assert ai_summary.summary["runtime_stage"]["stage_code"] == "waiting_ai"
    assert rows["task-startup-visible"]["runtime_stage"]["stage_code"] == "startup_checking"
    assert rows["task-startup-visible"]["runtime_stage"]["stage_label"] == "启动校验中"
    membership_stage = rows["task-membership-and-ai"]["runtime_stage"]
    assert membership_stage["stage_code"] == "membership_preparing"
    assert membership_stage["stage_label"] == "准入补齐中"
    assert "待准备 48" in membership_stage["reason"]
    assert "The read operation timed out" in membership_stage["reason"]


def test_operation_issue_detail_derives_current_task_runtime_stage_without_summary():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(
            Task(
                id="task-issue-current-stage",
                tenant_id=1,
                name="运营抽屉实时阶段",
                type="group_ai_chat",
                status="paused",
                last_error="人工暂停排查账号准入",
            )
        )
        issue = upsert_operation_issue(
            session,
            tenant_id=1,
            target_id=None,
            issue_type="task_execution_failure",
            failure_type="GROUP_PERMISSION_DENIED",
            source_task_id="task-issue-current-stage",
            representative_action_id="action-stage-visible",
            affected_account_ids=[],
            failure_reason="旧目标权限诊断",
            suggested_action="查看任务阶段",
        )
        session.commit()

        detail = get_operation_issue_detail(session, 1, issue.id)

    assert detail["related_task_summary"] is None
    assert detail["task_runtime_stage"]["stage_code"] == "paused"
    assert detail["task_runtime_stage"]["stage_label"] == "已暂停"
    assert "不会继续规划或执行新动作" in detail["task_runtime_stage"]["reason"]


def test_upsert_operation_issue_flushes_new_issue_before_children():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgAccount(id=31, tenant_id=1, display_name="异常账号", phone_masked="31", status=AccountStatus.ACTIVE.value))
        issue = upsert_operation_issue(
            session,
            tenant_id=1,
            target_id=None,
            issue_type="task_execution_failure",
            failure_type="GROUP_PERMISSION_DENIED",
            source_task_id="task-issue-flush",
            representative_action_id="action-issue-flush",
            affected_account_ids=[31],
            failure_reason="账号无权限",
            suggested_action="检查账号权限",
        )
        session.commit()
        issue_id = issue.id

        source = session.scalar(select(OperationIssueSource).where(OperationIssueSource.source_id == "task-issue-flush"))
        account = session.scalar(select(OperationIssueAccount).where(OperationIssueAccount.account_id == 31))

    assert issue_id
    assert source is not None
    assert source.issue_id == issue_id
    assert account is not None
    assert account.issue_id == issue_id


def test_sync_all_operation_targets_collects_every_online_account(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    snapshots_by_account = {
        11: [
            SimpleNamespace(tg_peer_id="-100shared", title="共同群", group_type="supergroup", member_count=120, permission_label="可发言", can_send=True, username="shared_group"),
            SimpleNamespace(tg_peer_id="-100only11", title="账号11独有群", group_type="supergroup", member_count=30, permission_label="可发言", can_send=True, username="only11"),
        ],
        12: [
            SimpleNamespace(tg_peer_id="-100shared", title="共同群", group_type="supergroup", member_count=121, permission_label="只读成员", can_send=False, username="shared_group"),
            SimpleNamespace(tg_peer_id="-100channel", title="账号12频道", group_type="channel", member_count=800, permission_label="可发帖", can_send=True, username="ops_channel"),
        ],
    }
    seen_accounts: list[int] = []

    def fake_list_groups(account_id: int, *_args, **_kwargs):
        seen_accounts.append(account_id)
        return snapshots_by_account[account_id]

    monkeypatch.setattr("app.services.operations.credentials_for_account", lambda *_args, **_kwargs: SimpleNamespace(api_id=1, api_hash="hash"))
    monkeypatch.setattr("app.services.operations.gateway.list_groups", fake_list_groups)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add_all(
            [
                TgAccount(id=11, tenant_id=1, display_name="账号11", phone_masked="11", status=AccountStatus.ACTIVE.value, session_ciphertext="session-11"),
                TgAccount(id=12, tenant_id=1, display_name="账号12", phone_masked="12", status=AccountStatus.ACTIVE.value, session_ciphertext="session-12"),
                TgAccount(id=13, tenant_id=1, display_name="离线账号", phone_masked="13", status=AccountStatus.NEED_RELOGIN.value, session_ciphertext=""),
            ]
        )
        session.commit()

        result = sync_all_operation_targets(session, 1, "pytest")
        shared_group = session.scalar(select(TgGroup).where(TgGroup.tg_peer_id == "-100shared"))
        shared_target = session.scalar(select(OperationTarget).where(OperationTarget.tg_peer_id == "-100shared"))
        shared_links = list(session.scalars(select(TgGroupAccount).where(TgGroupAccount.group_id == shared_group.id).order_by(TgGroupAccount.account_id.asc())))
        targets = filter_operation_targets(session, 1)

    assert seen_accounts == [11, 12]
    assert result["synced_accounts"] == 2
    assert result["failed_accounts"] == []
    assert result["target_count"] == 3
    assert {target["tg_peer_id"] for target in targets} == {"-100shared", "-100only11", "-100channel"}
    assert [link.account_id for link in shared_links] == [11, 12]
    assert [link.can_send for link in shared_links] == [True, False]
    assert shared_group.can_send is True
    assert shared_target.can_send is True


def test_drain_account_sync_records_staggers_all_session_accounts_hourly(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(engine, future=True)
    old_sync_time = _now() - timedelta(hours=2)
    processed_ids: list[int] = []

    def fake_process_account_sync_record(session: Session, record_id: int):
        processed_ids.append(record_id)
        record = session.get(TgAccountSyncRecord, record_id)
        assert record is not None
        record.status = "已同步"
        record.result_count = 1
        record.finished_at = _now()
        session.commit()
        return record

    monkeypatch.setattr(account_service, "process_account_sync_record", fake_process_account_sync_record)

    with SessionLocal() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add_all(
            [
                TgAccount(id=21, tenant_id=1, display_name="受限账号", phone_masked="21", status=AccountStatus.LIMITED.value, session_ciphertext="session-21"),
                TgAccount(id=22, tenant_id=1, display_name="在线账号", phone_masked="22", status=AccountStatus.ACTIVE.value, session_ciphertext="session-22"),
                TgAccount(id=23, tenant_id=1, display_name="需登录账号", phone_masked="23", status=AccountStatus.NEED_RELOGIN.value, session_ciphertext=""),
            ]
        )
        session.add_all(
            [
                TgAccountSyncRecord(
                    tenant_id=1,
                    account_id=21,
                    sync_type="health",
                    trigger_source="scheduled",
                    status="已同步",
                    scheduled_at=old_sync_time,
                    started_at=old_sync_time,
                    finished_at=old_sync_time,
                    created_at=old_sync_time,
                ),
                TgAccountSyncRecord(
                    tenant_id=1,
                    account_id=22,
                    sync_type="health",
                    trigger_source="scheduled",
                    status="已同步",
                    scheduled_at=old_sync_time,
                    started_at=old_sync_time,
                    finished_at=old_sync_time,
                    created_at=old_sync_time,
                ),
            ]
        )
        session.commit()

    processed_count = account_service.drain_account_sync_records(SessionLocal, limit=20)

    with SessionLocal() as session:
        limited_records = list(
            session.scalars(
                select(TgAccountSyncRecord)
                .where(TgAccountSyncRecord.account_id == 21, TgAccountSyncRecord.created_at > old_sync_time)
                .order_by(TgAccountSyncRecord.id.asc())
            )
        )
        active_records = list(
            session.scalars(
                select(TgAccountSyncRecord)
                .where(TgAccountSyncRecord.account_id == 22, TgAccountSyncRecord.created_at > old_sync_time)
                .order_by(TgAccountSyncRecord.id.asc())
            )
        )
        relogin_records = list(session.scalars(select(TgAccountSyncRecord).where(TgAccountSyncRecord.account_id == 23)))

    assert processed_count == 1
    assert len(processed_ids) == 1
    assert [record.sync_type for record in limited_records] == ["health"]
    assert [record.sync_type for record in active_records] == ["health"]
    assert all(record.status == "已同步" for record in limited_records)
    assert all(record.status == "排队中" for record in active_records)
    assert 2 <= (active_records[0].scheduled_at - limited_records[0].scheduled_at).total_seconds() <= 4
    assert relogin_records == []


def test_scheduling_setting_centralizes_quiet_hours_and_default_failure_policy():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        setting = update_scheduling_setting(
            session,
            1,
            SchedulingSettingUpdate(
                jitter_min_seconds=20,
                jitter_max_seconds=10,
                batch_interval_seconds=30,
                respect_send_window=True,
                quiet_hours_enabled=True,
                quiet_start="01:00",
                quiet_end="07:30",
                quiet_timezone="Asia/Shanghai",
                default_max_retries=5,
                default_retry_delay_seconds=90,
                default_retry_backoff="linear",
                default_on_account_banned="pause_task",
                default_on_api_rate_limit="pause",
                default_on_content_rejected="rewrite_and_retry",
                default_account_hour_limit=12,
                default_account_day_limit=80,
                default_account_cooldown_seconds=45,
            ),
            "pytest",
        )
        loaded = get_scheduling_setting(session, 1)

    assert setting.jitter_min_seconds == 20
    assert setting.jitter_max_seconds == 20
    assert loaded.quiet_hours_enabled is True
    assert loaded.quiet_start == "01:00"
    assert loaded.quiet_end == "07:30"
    assert loaded.default_max_retries == 5
    assert loaded.default_retry_delay_seconds == 90
    assert loaded.default_retry_backoff == "linear"
    assert loaded.default_on_account_banned == "pause_task"
    assert loaded.default_on_api_rate_limit == "pause"
    assert loaded.default_on_content_rejected == "rewrite_and_retry"
    assert loaded.default_account_hour_limit == 12
    assert loaded.default_account_day_limit == 80
    assert loaded.default_account_cooldown_seconds == 45


def test_risk_control_global_policy_updates_scheduling_policy():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        policy = update_global_policy(
            session,
            1,
            RiskControlGlobalPolicyUpdate(
                jitter_min_seconds=12,
                jitter_max_seconds=8,
                quiet_hours_enabled=True,
                default_on_api_rate_limit="pause",
                default_account_hour_limit=9,
            ),
            "pytest",
        )
        loaded = get_scheduling_setting(session, 1)

    assert policy["jitter_min_seconds"] == 12
    assert policy["jitter_max_seconds"] == 12
    assert policy["default_on_api_rate_limit"] == "pause"
    assert loaded.quiet_hours_enabled is True
    assert loaded.default_account_hour_limit == 9


def test_list_rule_sets_initializes_default_rule_center():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.commit()

        rule_sets = list_rule_sets(session, 1)
        loaded = list_rule_sets(session, 1)

    assert len(rule_sets) == 1
    assert len(loaded) == 1
    assert rule_sets[0].name == "默认运营规则集"
    assert set(rule_sets[0].task_types) == {"group_relay", "group_ai_chat", "channel_comment", "message_send"}
    assert rule_sets[0].active_version_id == rule_sets[0].versions[0].id
    assert rule_sets[0].versions[0].status == "published"
    assert rule_sets[0].versions[0].filters == {
        "keyword_whitelist": [],
        "keyword_blacklist": [],
        "min_message_length": None,
        "max_message_length": None,
        "allowed_media_types": [],
        "blocked_user_ids": [],
        "only_with_media": False,
        "only_text": False,
        "language_filter": None,
    }


def test_list_rule_sets_adds_default_even_when_custom_rule_set_exists():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        custom = RuleSet(tenant_id=1, name="自定义规则集", description="", status="active", task_types=["group_relay"], default_policy={})
        session.add(custom)
        session.commit()

        rule_sets = list_rule_sets(session, 1)

    assert [item.name for item in rule_sets] == ["自定义规则集", "默认运营规则集"]


def test_update_rule_set_config_auto_publishes_new_version():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.commit()
        rule_set = list_rule_sets(session, 1)[0]

        updated = update_rule_set_config(
            session,
            1,
            rule_set.id,
            RuleSetVersionCreate(
                filters={"keyword_blacklist": ["广告"]},
                output_checks={"forbidden_keywords": ["联系我"], "failure_strategy": "drop"},
                transforms={"remove_links": True},
                routing={},
                account_strategy={"mode": "target_sticky"},
                rate_limits={},
                retry_policy={"max_retries": 1},
                version_note="调整基础过滤",
            ),
            "pytest",
        )

    versions = sorted(updated.versions, key=lambda item: item.version)
    assert [item.version for item in versions] == [1, 2]
    assert versions[0].status == "archived"
    assert versions[1].status == "published"
    assert updated.active_version_id == versions[1].id
    assert versions[1].filters["keyword_blacklist"] == ["广告"]
    assert versions[1].transforms["remove_links"] is True


def test_prompt_template_and_material_updates_are_persisted():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.commit()
        template = create_prompt_template(
            session,
            PromptTemplateCreate(tenant_id=1, template_type="群活跃对话计划", name="旧提示词", content="旧内容"),
            "pytest",
        )
        material = create_material(
            session,
            MaterialCreate(tenant_id=1, title="旧素材", material_type="图片", content="https://old.example/a.png", tags="旧"),
            "pytest",
        )

        updated_template = update_prompt_template(
            session,
            template.id,
            PromptTemplateUpdate(template_type="素材配文", name="新提示词", content="新内容", is_active=False),
            "pytest",
        )
        updated_material = update_material(
            session,
            material.id,
            MaterialUpdate(title="新素材", material_type="链接", content="https://new.example/b", tags="新"),
            "pytest",
        )
        updated_template_snapshot = {
            "name": updated_template.name,
            "template_type": updated_template.template_type,
            "content": updated_template.content,
            "is_active": updated_template.is_active,
            "version": updated_template.version,
        }
        updated_material_snapshot = {
            "title": updated_material.title,
            "material_type": updated_material.material_type,
            "content": updated_material.content,
            "tags": updated_material.tags,
        }

    assert updated_template_snapshot == {
        "name": "新提示词",
        "template_type": "素材配文",
        "content": "新内容",
        "is_active": False,
        "version": 2,
    }
    assert updated_material_snapshot == {
        "title": "新素材",
        "material_type": "链接",
        "content": "https://new.example/b",
        "tags": "新",
    }


def test_account_capacity_counts_task_center_and_message_send_records():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        now_value = datetime.now(UTC).replace(tzinfo=None)
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(SchedulingSetting(tenant_id=1, default_account_hour_limit=2, default_account_day_limit=10, jitter_min_seconds=0, jitter_max_seconds=0))
        session.add(TgAccount(id=11, tenant_id=1, display_name="发送号", phone_masked="11", status=AccountStatus.ACTIVE.value))
        session.add(Task(id="task-capacity", tenant_id=1, name="容量", type="group_ai_chat", status="running"))
        session.add_all(
            [
                Action(id="action-success", tenant_id=1, task_id="task-capacity", task_type="group_ai_chat", action_type="send_message", account_id=11, status="success", scheduled_at=now_value, executed_at=now_value),
                Action(id="action-failed", tenant_id=1, task_id="task-capacity", task_type="group_ai_chat", action_type="send_message", account_id=11, status="failed", scheduled_at=now_value),
                MessageTask(tenant_id=1, account_id=11, preferred_account_id=11, content="排队消息", status=TaskStatus.QUEUED.value, scheduled_at=now_value, idempotency_key="queued-11"),
                MessageTask(tenant_id=1, account_id=11, preferred_account_id=11, content="取消消息", status=TaskStatus.CANCELLED.value, scheduled_at=now_value, idempotency_key="cancelled-11"),
            ]
        )
        session.commit()

        decision = account_capacity_decision(session, tenant_id=1, account_id=11, scheduled_at=now_value)

    assert decision.available is False
    assert decision.reason_code == "account_hour_limit"


def test_account_capacity_normalizes_aware_last_occupied_for_reservation_cooldown(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    scheduled_at = datetime(2026, 6, 8, 20, 10)
    aware_last_at = datetime(2026, 6, 8, 20, 9, tzinfo=BEIJING_TZ)

    monkeypatch.setattr("app.services.account_capacity._last_occupied_at", lambda *_args, **_kwargs: aware_last_at)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(SchedulingSetting(tenant_id=1, default_account_cooldown_seconds=120, jitter_min_seconds=0, jitter_max_seconds=0))
        session.commit()

        decision = account_capacity_decision(
            session,
            tenant_id=1,
            account_id=11,
            scheduled_at=scheduled_at,
            reservations=[AccountCapacityReservation(account_id=11, scheduled_at=scheduled_at)],
        )

    assert decision.available is False
    assert decision.reason_code == "account_cooldown"
    assert decision.defer_until == datetime(2026, 6, 8, 20, 12)


def test_account_capacity_cache_reuses_cooldown_lookups(monkeypatch):
    from app.services import account_capacity as capacity_service

    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    scheduled_at = datetime(2026, 6, 8, 20, 10)
    calls = {"last": 0, "next": 0}
    original_last = capacity_service._last_occupied_at
    original_next = capacity_service._next_occupied_at

    def counted_last(*args, **kwargs):  # noqa: ANN002, ANN003
        calls["last"] += 1
        return original_last(*args, **kwargs)

    def counted_next(*args, **kwargs):  # noqa: ANN002, ANN003
        calls["next"] += 1
        return original_next(*args, **kwargs)

    monkeypatch.setattr(capacity_service, "_last_occupied_at", counted_last)
    monkeypatch.setattr(capacity_service, "_next_occupied_at", counted_next)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(SchedulingSetting(tenant_id=1, default_account_cooldown_seconds=120, jitter_min_seconds=0, jitter_max_seconds=0))
        session.commit()

        cache = AccountCapacityCache()
        first = account_capacity_decision(session, tenant_id=1, account_id=11, scheduled_at=scheduled_at, cache=cache)
        second = account_capacity_decision(session, tenant_id=1, account_id=11, scheduled_at=scheduled_at, cache=cache)

    assert first.available is True
    assert second.available is True
    assert calls == {"last": 1, "next": 0}
    assert len(cache.occupied_timelines) == 1


def test_account_capacity_cache_reuses_future_timeline_for_adjacent_slots(monkeypatch):
    from app.services import account_capacity as capacity_service

    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    scheduled_at = datetime(2026, 6, 8, 20, 10)

    def fail_precise_next_lookup(*_args, **_kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("cached capacity checks should use the hourly timeline")

    monkeypatch.setattr(capacity_service, "_next_occupied_at", fail_precise_next_lookup)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(SchedulingSetting(tenant_id=1, default_account_cooldown_seconds=120, jitter_min_seconds=0, jitter_max_seconds=0))
        session.add(
            Action(
                id="future-action-11",
                tenant_id=1,
                task_id="task-capacity",
                task_type="group_ai_chat",
                action_type="send_message",
                account_id=11,
                status="pending",
                scheduled_at=scheduled_at + timedelta(seconds=60),
                payload={},
            )
        )
        session.commit()

        cache = AccountCapacityCache()
        first = account_capacity_decision(session, tenant_id=1, account_id=11, scheduled_at=scheduled_at, cache=cache)
        second = account_capacity_decision(
            session,
            tenant_id=1,
            account_id=11,
            scheduled_at=scheduled_at + timedelta(seconds=10),
            cache=cache,
        )

    assert first.available is False
    assert first.defer_until == scheduled_at + timedelta(seconds=180)
    assert second.available is False
    assert second.defer_until == scheduled_at + timedelta(seconds=180)
    assert len(cache.occupied_timelines) == 1


def test_task_center_dispatch_reassigns_when_account_limit_reached(monkeypatch):
    from app.services.task_center import dispatcher

    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    sent: dict[str, int] = {}

    def fake_send(account_id, *_args, **_kwargs):  # noqa: ANN001
        sent["account_id"] = account_id
        return SendResult(True, remote_message_id="reassigned-ok")

    with Session(engine) as session:
        now_value = datetime.now(UTC).replace(tzinfo=None)
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(SchedulingSetting(tenant_id=1, default_account_hour_limit=1, jitter_min_seconds=0, jitter_max_seconds=0))
        session.add_all(
            [
                TgAccount(id=11, tenant_id=1, display_name="满额号", phone_masked="11", status=AccountStatus.ACTIVE.value, health_score=90),
                TgAccount(id=12, tenant_id=1, display_name="备用号", phone_masked="12", status=AccountStatus.ACTIVE.value, health_score=80),
                TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="运营群", auth_status="已授权运营", can_send=True, daily_limit=999),
                TgGroupAccount(tenant_id=1, group_id=7, account_id=11, can_send=True),
                TgGroupAccount(tenant_id=1, group_id=7, account_id=12, can_send=True),
                Task(id="task-reassign", tenant_id=1, name="转派", type="group_ai_chat", status="running", account_config={"selection_mode": "all", "max_concurrent": 2, "cooldown_per_account_minutes": 0}),
                Action(id="action-used", tenant_id=1, task_id="task-reassign", task_type="group_ai_chat", action_type="send_message", account_id=11, status="success", scheduled_at=now_value, executed_at=now_value),
                Action(id="action-send", tenant_id=1, task_id="task-reassign", task_type="group_ai_chat", action_type="send_message", account_id=11, status="pending", scheduled_at=now_value, payload={"group_id": 7, "message_text": "需要转派", "review_approved": True}, result={}),
            ]
        )
        session.commit()

        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *args, **kwargs: object())
        monkeypatch.setattr(dispatcher.gateway, "send_message", fake_send)
        action = session.get(Action, "action-send")
        assert dispatcher.dispatch_action(session, action) is True

        assert sent["account_id"] == 12
        assert action.account_id == 12
        assert action.status == "success"
        assert action.result["original_account_id"] == 11
        assert action.result["reassigned_account_id"] == 12
        assert action.result["telegram_msg_id"] == "reassigned-ok"


def _channel_comment_action(action_id: str, comment_text: str, scheduled_at: datetime) -> Action:
    return Action(
        id=action_id,
        tenant_id=1,
        task_id="task-comment-permission",
        task_type="channel_comment",
        action_type="post_comment",
        account_id=11,
        status="pending",
        scheduled_at=scheduled_at,
        payload={
            "channel_id": "-10031",
            "channel_target_id": 31,
            "channel_message_id": 41,
            "message_id": 7301,
            "message_content": "招生信息",
            "comment_text": comment_text,
            "target_display": "天津音乐学院频道",
        },
        result={},
    )


def _channel_like_action(action_id: str, account_id: int, scheduled_at: datetime) -> Action:
    return Action(
        id=action_id,
        tenant_id=1,
        task_id="task-like-unavailable",
        task_type="channel_like",
        action_type="like_message",
        account_id=account_id,
        status="pending",
        scheduled_at=scheduled_at,
        payload={
            "channel_id": "-10031",
            "channel_target_id": 31,
            "channel_message_id": 41,
            "message_id": 7301,
            "message_content": "招生信息",
            "reaction_emoji": "👍",
            "target_display": "天津音乐学院频道",
        },
        result={},
    )


def test_channel_like_reaction_unavailable_skips_message_siblings(monkeypatch):
    from app.services.task_center import dispatcher

    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        now_value = datetime.now(UTC).replace(tzinfo=None)
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add_all(
            [
                TgAccount(id=11, tenant_id=1, display_name="点赞号1", phone_masked="11", status=AccountStatus.ACTIVE.value, session_ciphertext="session-11"),
                TgAccount(id=12, tenant_id=1, display_name="点赞号2", phone_masked="12", status=AccountStatus.ACTIVE.value, session_ciphertext="session-12"),
                OperationTarget(id=31, tenant_id=1, target_type="channel", tg_peer_id="-10031", title="天津音乐学院频道", can_send=True, auth_status="已授权运营"),
                ChannelMessage(id=41, tenant_id=1, channel_target_id=31, message_id=7301, content_preview="招生信息", comment_available=True),
                Task(id="task-like-unavailable", tenant_id=1, name="频道点赞", type="channel_like", status="running"),
                _channel_like_action("action-like-main", 11, now_value),
                _channel_like_action("action-like-sibling", 12, now_value),
            ]
        )
        session.commit()

        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *args, **kwargs: object())
        monkeypatch.setattr(
            dispatcher.gateway,
            "send_channel_reaction",
            lambda *args, **kwargs: OperationResult(False, "失败", FailureType.REACTION_UNAVAILABLE.value, "频道消息不可点赞或消息ID无效"),
        )
        action = session.get(Action, "action-like-main")
        assert dispatcher.dispatch_action(session, action) is True

        sibling = session.get(Action, "action-like-sibling")
        assert action.status == "skipped"
        assert action.result["error_code"] == "reaction_unavailable_message"
        assert sibling.status == "skipped"
        assert sibling.result["error_code"] == "reaction_unavailable_sibling"


def test_post_comment_permission_denied_blocks_account_and_skips_account_siblings(monkeypatch):
    from app.services.task_center import dispatcher

    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        now_value = datetime.now(UTC).replace(tzinfo=None)
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add_all(
            [
                TgAccount(id=11, tenant_id=1, display_name="评论号", phone_masked="11", status=AccountStatus.ACTIVE.value, session_ciphertext="session-11"),
                OperationTarget(id=31, tenant_id=1, target_type="channel", tg_peer_id="-10031", title="天津音乐学院频道", can_send=True, auth_status="已授权运营"),
                TgGroup(id=32, tenant_id=1, tg_peer_id="-10031", title="天津音乐学院频道", group_type="channel", auth_status="已授权运营", can_send=True),
                TgGroupAccount(tenant_id=1, group_id=32, account_id=11, can_send=True),
                ChannelMessage(id=41, tenant_id=1, channel_target_id=31, message_id=7301, content_preview="招生信息", comment_available=True),
                Task(id="task-comment-permission", tenant_id=1, name="频道评论", type="channel_comment", status="running"),
                _channel_comment_action("action-comment-main", "想了解一下今年的招生安排", now_value),
                _channel_comment_action("action-comment-sibling", "这个信息很实用", now_value),
            ]
        )
        session.commit()

        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *args, **kwargs: object())
        monkeypatch.setattr(
            dispatcher.gateway,
            "reply_channel_message",
            lambda *args, **kwargs: SendResult(False, failure_type=FailureType.GROUP_PERMISSION_DENIED.value, detail="群无权限或账号不可发言"),
        )
        action = session.get(Action, "action-comment-main")
        assert dispatcher.dispatch_action(session, action) is True

        sibling = session.get(Action, "action-comment-sibling")
        message = session.get(ChannelMessage, 41)
        link = session.scalar(select(TgGroupAccount).where(TgGroupAccount.account_id == 11))
        assert action.status == "skipped"
        assert action.result["error_code"] == "comment_account_permission_denied"
        assert sibling.status == "skipped"
        assert sibling.result["error_code"] == "comment_account_permission_denied"
        assert link.can_send is False
        assert message.comment_available is True


def test_post_comment_without_membership_creates_membership_and_defers(monkeypatch):
    from app.services.task_center import dispatcher

    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        now_value = datetime.now(UTC).replace(tzinfo=None)
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add_all(
            [
                TgAccount(id=11, tenant_id=1, display_name="评论号", phone_masked="11", status=AccountStatus.ACTIVE.value, session_ciphertext="session-11"),
                OperationTarget(id=31, tenant_id=1, target_type="channel", tg_peer_id="-10031", title="天津音乐学院频道", can_send=True, auth_status="已授权运营"),
                ChannelMessage(id=41, tenant_id=1, channel_target_id=31, message_id=7301, content_preview="招生信息", comment_available=True),
                Task(id="task-comment-membership", tenant_id=1, name="频道评论", type="channel_comment", status="running"),
                _channel_comment_action("action-comment-main", "想了解一下今年的招生安排", now_value),
            ]
        )
        session.get(Action, "action-comment-main").task_id = "task-comment-membership"
        session.commit()

        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *args, **kwargs: object())
        monkeypatch.setattr(dispatcher.gateway, "reply_channel_message", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("comment must wait for membership")))
        action = session.get(Action, "action-comment-main")
        assert dispatcher.dispatch_action(session, action) is True

        membership = session.scalar(select(Action).where(Action.task_id == "task-comment-membership", Action.action_type == "ensure_target_membership"))
        assert action.status == "pending"
        assert action.scheduled_at > now_value
        assert action.result["error_code"] == "comment_membership_required"
        assert membership is not None
        assert membership.account_id == 11
        assert membership.payload["channel_target_id"] == 31
        assert membership.payload["require_send"] is True


def test_post_comment_membership_error_requeues_membership_even_with_stale_link(monkeypatch):
    from app.services.task_center import dispatcher

    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        now_value = datetime.now(UTC).replace(tzinfo=None)
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add_all(
            [
                TgAccount(id=11, tenant_id=1, display_name="评论号", phone_masked="11", status=AccountStatus.ACTIVE.value, session_ciphertext="session-11"),
                OperationTarget(id=31, tenant_id=1, target_type="channel", tg_peer_id="-10031", title="天津音乐学院频道", can_send=True, auth_status="已授权运营"),
                TgGroup(id=32, tenant_id=1, tg_peer_id="-10031", title="天津音乐学院频道", group_type="channel", auth_status="已授权运营", can_send=True),
                TgGroupAccount(tenant_id=1, group_id=32, account_id=11, can_send=True),
                ChannelMessage(id=41, tenant_id=1, channel_target_id=31, message_id=7301, content_preview="招生信息", comment_available=True),
                Task(id="task-comment-stale-membership", tenant_id=1, name="频道评论", type="channel_comment", status="running"),
                _channel_comment_action("action-comment-main", "想了解一下今年的招生安排", now_value),
            ]
        )
        session.get(Action, "action-comment-main").task_id = "task-comment-stale-membership"
        session.commit()

        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *args, **kwargs: object())
        monkeypatch.setattr(
            dispatcher.gateway,
            "reply_channel_message",
            lambda *args, **kwargs: SendResult(False, failure_type=FailureType.GROUP_PERMISSION_DENIED.value, detail="账号未关注/未加入目标频道或无法进入关联讨论区"),
        )
        action = session.get(Action, "action-comment-main")
        assert dispatcher.dispatch_action(session, action) is True

        membership = session.scalar(select(Action).where(Action.task_id == "task-comment-stale-membership", Action.action_type == "ensure_target_membership"))
        assert action.status == "pending"
        assert action.result["error_code"] == "comment_membership_required"
        assert membership is not None
        assert membership.payload["require_send"] is True


def test_post_comment_unavailable_marks_message_and_skips_siblings(monkeypatch):
    from app.services.task_center import dispatcher

    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        now_value = datetime.now(UTC).replace(tzinfo=None)
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add_all(
            [
                TgAccount(id=11, tenant_id=1, display_name="评论号", phone_masked="11", status=AccountStatus.ACTIVE.value, session_ciphertext="session-11"),
                OperationTarget(id=31, tenant_id=1, target_type="channel", tg_peer_id="-10031", title="天津音乐学院频道", can_send=True, auth_status="已授权运营"),
                TgGroup(id=32, tenant_id=1, tg_peer_id="-10031", title="天津音乐学院频道", group_type="channel", auth_status="已授权运营", can_send=True),
                TgGroupAccount(tenant_id=1, group_id=32, account_id=11, can_send=True),
                ChannelMessage(id=41, tenant_id=1, channel_target_id=31, message_id=7301, content_preview="招生信息", comment_available=True),
                Task(id="task-comment-unavailable", tenant_id=1, name="频道评论", type="channel_comment", status="running"),
                _channel_comment_action("action-comment-main", "想了解一下今年的招生安排", now_value),
                _channel_comment_action("action-comment-sibling", "这个信息很实用", now_value),
            ]
        )
        for action_id in ["action-comment-main", "action-comment-sibling"]:
            session.get(Action, action_id).task_id = "task-comment-unavailable"
        session.commit()

        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *args, **kwargs: object())
        monkeypatch.setattr(
            dispatcher.gateway,
            "reply_channel_message",
            lambda *args, **kwargs: SendResult(False, failure_type=FailureType.COMMENT_UNAVAILABLE.value, detail="频道帖子无法解析到评论区"),
        )
        action = session.get(Action, "action-comment-main")
        assert dispatcher.dispatch_action(session, action) is True

        sibling = session.get(Action, "action-comment-sibling")
        message = session.get(ChannelMessage, 41)
        assert action.status == "skipped"
        assert action.result["error_code"] == "comment_unavailable_message"
        assert sibling.status == "skipped"
        assert sibling.result["error_code"] == "comment_unavailable_sibling"
        assert message.comment_available is False


def test_message_send_reassigns_group_and_defers_private_when_account_limit_reached(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    SessionFactory = sessionmaker(bind=engine)
    sent_accounts: list[int] = []

    def fake_send(account_id, *_args, **_kwargs):  # noqa: ANN001
        sent_accounts.append(account_id)
        return SendResult(True, remote_message_id=f"sent-{account_id}")

    monkeypatch.setattr("app.services.messages.credentials_for_account", lambda *args, **kwargs: object())
    monkeypatch.setattr("app.services.messages.gateway.send_message", fake_send)

    with SessionFactory() as session:
        now_value = datetime.now(UTC).replace(tzinfo=None)
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(SchedulingSetting(tenant_id=1, default_account_hour_limit=1, jitter_min_seconds=0, jitter_max_seconds=0))
        session.add_all(
            [
                TgAccount(id=11, tenant_id=1, display_name="满额号", phone_masked="11", status=AccountStatus.ACTIVE.value, health_score=90, session_ciphertext="s11"),
                TgAccount(id=12, tenant_id=1, display_name="备用号", phone_masked="12", status=AccountStatus.ACTIVE.value, health_score=80, session_ciphertext="s12"),
                TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="运营群", auth_status="已授权运营", can_send=True, daily_limit=999),
                TgGroupAccount(tenant_id=1, group_id=7, account_id=11, can_send=True),
                TgGroupAccount(tenant_id=1, group_id=7, account_id=12, can_send=True),
                MessageTask(tenant_id=1, account_id=11, preferred_account_id=11, group_id=7, content="已占用", status=TaskStatus.SENT.value, sent_at=now_value, scheduled_at=now_value, idempotency_key="used-11"),
            ]
        )
        group_task = MessageTask(tenant_id=1, account_id=11, preferred_account_id=11, group_id=7, target_type="group", target_peer_id="-1007", target_display="运营群", content="群消息", status=TaskStatus.QUEUED.value, scheduled_at=now_value, idempotency_key="group-reassign")
        private_task = MessageTask(tenant_id=1, account_id=11, preferred_account_id=11, target_type="private", target_peer_id="@demo", target_display="联系人", content="私聊消息", status=TaskStatus.QUEUED.value, scheduled_at=now_value, idempotency_key="private-defer")
        session.add_all([group_task, private_task])
        session.commit()
        group_task_id = group_task.id
        private_task_id = private_task.id

    dispatch_task(SessionFactory, group_task_id)
    dispatch_task(SessionFactory, private_task_id)

    with SessionFactory() as session:
        group_task = session.get(MessageTask, group_task_id)
        private_task = session.get(MessageTask, private_task_id)
        transfer_attempt = session.scalar(select(MessageTaskAttempt).where(MessageTaskAttempt.task_id == group_task_id, MessageTaskAttempt.status == TaskStatus.QUEUED.value))

    assert sent_accounts == [12]
    assert group_task.status == TaskStatus.SENT.value
    assert group_task.account_id == 12
    assert group_task.preferred_account_id == 11
    assert group_task.actual_account_changed is True
    assert transfer_attempt and "转派" in transfer_attempt.detail
    assert private_task.status == TaskStatus.QUEUED.value
    assert private_task.scheduled_at > datetime.now(UTC).replace(tzinfo=None)
    assert private_task.failure_type == "account_hour_limit"


def test_telethon_send_target_marks_legacy_basic_group_ids():
    assert _telethon_send_target("5129187268", group_id=16) == -5129187268
    assert _telethon_send_target("-1003984659798", group_id=16) == -1003984659798
    assert _telethon_send_target("5129187268", group_id=0) == 5129187268
    assert _telethon_send_target("@demo_group", group_id=16) == "@demo_group"


def test_gateway_maps_join_channel_permission_denied():
    result = TelethonTelegramGateway._map_send_error(
        Exception("The channel specified is private and you lack permission to access it. Another reason may be that you were banned from it (caused by JoinChannelRequest)")
    )

    assert result.failure_type == FailureType.GROUP_PERMISSION_DENIED.value
    assert result.detail == "群无权限或账号不可发言"


def test_gateway_maps_join_request_pending_to_permission_denied():
    result = TelethonTelegramGateway._map_send_error(
        Exception("You have successfully requested to join this chat or channel (caused by JoinChannelRequest)")
    )

    assert result.failure_type == FailureType.GROUP_PERMISSION_DENIED.value
    assert result.detail == "已提交入群申请，等待审批后才能发言"


def test_telethon_resolve_uses_migrated_target_for_basic_groups():
    from telethon.tl import types

    migrated = types.InputChannel(channel_id=3562550107, access_hash=2248416258286237861)
    legacy = types.Chat(
        id=5129187268,
        title="legacy group",
        photo=types.ChatPhotoEmpty(),
        participants_count=0,
        date=None,
        version=1,
        deactivated=True,
        migrated_to=migrated,
    )

    class FakeClient:
        async def get_entity(self, target):
            assert target == -5129187268
            return legacy

    assert asyncio.run(_resolve_telethon_target(FakeClient(), "-5129187268", group_id=16)) is migrated


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


def test_task_center_dispatch_defers_by_global_account_policy(monkeypatch):
    from app.services.task_center import dispatcher

    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        now_value = datetime.now(UTC).replace(tzinfo=None)
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(SchedulingSetting(tenant_id=1, default_account_cooldown_seconds=120, default_account_hour_limit=1))
        session.add(TgAccount(id=11, tenant_id=1, display_name="发送号", phone_masked="+861***0011", status="在线"))
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1001", title="运营群", auth_status="已授权运营", can_send=True))
        session.add(TgGroupAccount(tenant_id=1, group_id=7, account_id=11, can_send=True))
        session.add(Task(id="task-policy", tenant_id=1, name="账号策略", type="group_ai_chat", status="running"))
        session.add(
            Action(
                id="action-success",
                tenant_id=1,
                task_id="task-policy",
                task_type="group_ai_chat",
                action_type="send_message",
                account_id=11,
                status="success",
                scheduled_at=now_value,
                executed_at=now_value,
                payload={"group_id": 7, "message_text": "上一条", "review_approved": True},
            )
        )
        session.add(
            Action(
                id="action-deferred",
                tenant_id=1,
                task_id="task-policy",
                task_type="group_ai_chat",
                action_type="send_message",
                account_id=11,
                status="pending",
                scheduled_at=now_value,
                payload={"group_id": 7, "message_text": "下一条", "review_approved": True},
                result={},
            )
        )
        session.commit()

        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("deferred action must not send")))
        action = session.get(Action, "action-deferred")
        assert dispatcher.dispatch_action(session, action) is True

        assert action.status == "pending"
        assert action.scheduled_at > now_value
        assert action.result["error_code"] == "global_account_policy"
        assert action.result["validation_stage"] == "account_policy"


def test_task_center_dispatch_applies_default_failure_policy(monkeypatch):
    from app.services.task_center import dispatcher

    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(SchedulingSetting(tenant_id=1, default_on_account_banned="pause_task", default_on_api_rate_limit="wait_and_retry"))
        session.add(TgAccount(id=11, tenant_id=1, display_name="发送号", phone_masked="+861***0011", status="在线"))
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1001", title="运营群", auth_status="已授权运营", can_send=True, daily_limit=999))
        session.add(TgGroupAccount(tenant_id=1, group_id=7, account_id=11, can_send=True))
        session.add(Task(id="task-failure-policy", tenant_id=1, name="失败策略", type="group_ai_chat", status="running"))
        session.add(
            Action(
                id="action-account-limited",
                tenant_id=1,
                task_id="task-failure-policy",
                task_type="group_ai_chat",
                action_type="send_message",
                account_id=11,
                status="pending",
                payload={"group_id": 7, "message_text": "触发受限", "review_approved": True},
                result={},
            )
        )
        session.commit()

        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *args, **kwargs: object())
        monkeypatch.setattr(dispatcher.gateway, "send_message", lambda *args, **kwargs: SendResult(False, failure_type=FailureType.ACCOUNT_LIMITED.value, detail="账号受限"))
        action = session.get(Action, "action-account-limited")
        assert dispatcher.dispatch_action(session, action) is True
        task = session.get(Task, "task-failure-policy")

        assert action.status == "failed"
        assert task.status == "paused"
        assert task.stats["last_failure_policy"] == "pause_task"
        assert session.get(TgAccount, 11).status == AccountStatus.LIMITED.value
        assert action.result["error_message"] == "账号受限"

        task.status = "running"
        task.next_run_at = None
        session.add(
            Action(
                id="action-flood-wait",
                tenant_id=1,
                task_id="task-failure-policy",
                task_type="group_ai_chat",
                action_type="send_message",
                account_id=11,
                status="pending",
                payload={"group_id": 7, "message_text": "触发限流", "review_approved": True},
                result={},
            )
        )
        session.get(TgAccount, 11).status = "在线"
        session.commit()
        before = _now()
        monkeypatch.setattr(dispatcher.gateway, "send_message", lambda *args, **kwargs: SendResult(False, failure_type=FailureType.FLOOD_WAIT.value, detail="FloodWait 120 秒"))
        flood_action = session.get(Action, "action-flood-wait")
        assert dispatcher.dispatch_action(session, flood_action) is True

        assert flood_action.status == "pending"
        assert flood_action.executed_at is None
        assert flood_action.scheduled_at >= before + timedelta(seconds=120)
        assert flood_action.result["validation_stage"] == "failure_policy"
        assert flood_action.result["retry_after_seconds"] == 120

        setting = session.scalar(select(SchedulingSetting).where(SchedulingSetting.tenant_id == 1))
        setting.default_on_content_rejected = "rewrite_and_retry"
        task.status = "running"
        session.add(ContentKeywordRule(tenant_id=1, keyword="违规词"))
        session.add(
            Action(
                id="action-content-rejected",
                tenant_id=1,
                task_id="task-failure-policy",
                task_type="group_ai_chat",
                action_type="send_message",
                account_id=11,
                status="pending",
                payload={"group_id": 7, "message_text": "包含违规词", "review_approved": True},
                result={},
            )
        )
        session.commit()

        content_action = session.get(Action, "action-content-rejected")
        assert dispatcher.dispatch_action(session, content_action) is True

        assert content_action.status == "pending"
        assert content_action.retry_count == 1
        assert "违规词" not in content_action.payload["message_text"]
        assert content_action.result["failure_policy_action"] == "rewrite_and_retry"
        assert content_action.result["auto_check"] == "延后"
        assert task.stats["last_failure_policy"] == "rewrite_and_retry"

        monkeypatch.setattr(dispatcher.gateway, "send_message", lambda *args, **kwargs: SendResult(True, remote_message_id="tg-rewritten"))
        assert dispatcher.dispatch_action(session, content_action) is True

        assert content_action.status == "success"
        assert content_action.result["telegram_msg_id"] == "tg-rewritten"

        setting.default_on_account_banned = "stop_task"
        task.status = "running"
        session.add_all(
            [
                Action(
                    id="action-account-missing",
                    tenant_id=1,
                    task_id="task-failure-policy",
                    task_type="group_ai_chat",
                    action_type="send_message",
                    account_id=999,
                    status="pending",
                    payload={"group_id": 7, "message_text": "账号失效", "review_approved": True},
                    result={},
                ),
                Action(
                    id="action-after-stop",
                    tenant_id=1,
                    task_id="task-failure-policy",
                    task_type="group_ai_chat",
                    action_type="send_message",
                    account_id=11,
                    status="pending",
                    payload={"group_id": 7, "message_text": "不应继续发送", "review_approved": True},
                    result={},
                ),
            ]
        )
        session.commit()

        missing_action = session.get(Action, "action-account-missing")
        assert dispatcher.dispatch_action(session, missing_action) is True

        assert task.status == "stopped"
        assert missing_action.status == "failed"
        assert session.get(Action, "action-after-stop").status == "skipped"
        assert task.stats["last_failure_policy"] == "stop_task"


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
                TgGroup(id=8, tenant_id=1, tg_peer_id="-1008", title="活跃群", auth_status="已授权运营", listener_enabled=False),
                TgAccount(id=11, tenant_id=1, display_name="频道账号A", username="channel_a", phone_masked="11", status="在线", health_score=90),
                TgAccount(id=12, tenant_id=1, display_name="监听账号A", username="listener_a", phone_masked="12", status="在线", health_score=80),
                TgAccount(id=13, tenant_id=1, display_name="监听账号B", username="listener_b", phone_masked="13", status="离线", health_score=70),
                TgAccount(id=14, tenant_id=1, display_name="AI账号", username="ai_user", phone_masked="14", status="在线", health_score=95),
                TgAccount(id=15, tenant_id=1, display_name="草稿账号", username="draft_user", phone_masked="15", status="在线", health_score=60),
                TgGroupAccount(id=71, tenant_id=1, group_id=7, account_id=12, is_listener=True),
                TgGroupAccount(id=72, tenant_id=1, group_id=7, account_id=13, is_listener=True),
                TgGroupAccount(id=73, tenant_id=1, group_id=7, account_id=14, can_send=True),
                TgGroupAccount(id=81, tenant_id=1, group_id=8, account_id=14, can_send=True),
                GroupContextMessage(id=41, tenant_id=1, group_id=7, listener_account_id=12, sender_name="用户", content="源群事件", remote_message_id="m1", sent_at=datetime(2026, 5, 11, 10, 0, 0)),
                MessageFingerprint(tenant_id=1, source_group_id="task-relay:relay:7:target:8", fingerprint="dedup-1", original_text="源群事件"),
                Task(id="task-channel", tenant_id=1, name="频道任务", type="channel_like", status="running", account_config={"account_ids": [11]}, type_config={"target_channel_id": 21}),
                Task(id="task-channel-draft", tenant_id=1, name="草稿频道任务", type="channel_like", status="draft", account_config={"account_ids": [15]}, type_config={"target_channel_id": 21}),
                Task(id="task-channel-paused", tenant_id=1, name="暂停频道任务", type="channel_like", status="paused", account_config={"account_ids": [15]}, type_config={"target_channel_id": 21}),
                Task(id="task-channel-completed", tenant_id=1, name="完成频道任务", type="channel_like", status="completed", account_config={"account_ids": [15]}, type_config={"target_channel_id": 21}),
                Task(id="task-channel-failed", tenant_id=1, name="失败频道任务", type="channel_like", status="failed", account_config={"account_ids": [15]}, type_config={"target_channel_id": 21}),
                Task(id="task-ai", tenant_id=1, name="AI 活跃任务", type="group_ai_chat", status="pending", account_config={"selection_mode": "manual", "account_ids": [14]}, type_config={"target_group_id": 8, "history_fetch_account_id": 14}),
                Task(id="task-relay", tenant_id=1, name="转发任务", type="group_relay", status="running", type_config={"source_groups": [{"group_id": 7, "is_active": True}], "monitor_account_ids": [12, 13]}),
                Action(id="action-channel", tenant_id=1, task_id="task-channel", task_type="channel_like", action_type="like_message", status="pending"),
                Action(id="action-relay", tenant_id=1, task_id="task-relay", task_type="group_relay", action_type="send_message", status="executing"),
            ]
        )
        session.commit()

        summary = listener_summary(session, 1)

    rows = {item.key: item for item in summary.items}
    assert rows["channel:21"].subscriber_task_count == 1
    assert rows["channel:21"].listener_account_count == 1
    assert rows["channel:21"].task_ids == ["task-channel"]
    assert [task.id for task in rows["channel:21"].subscriber_tasks] == ["task-channel"]
    assert [(account.id, account.roles, account.task_ids) for account in rows["channel:21"].listener_accounts] == [(11, ["点赞账号"], ["task-channel"])]
    assert rows["channel:21"].event_backlog_count == 1
    assert rows["channel:21"].pending_distribution_count == 1
    assert rows["channel:21"].dedup_event_count == 1
    assert rows["channel:21"].subscription_event_types == ["频道消息", "Reaction"]
    assert rows["channel:21"].last_event_at == "2026-05-11T09:00:00"
    assert rows["channel:21"].recent_events[0].content == "频道消息"
    assert rows["channel:21"].backup_account.id == 14
    assert rows["group:7"].subscriber_task_count == 1
    assert rows["group:7"].listener_account_count == 2
    assert [(account.id, account.status, account.roles, account.task_ids) for account in rows["group:7"].listener_accounts] == [
        (12, "在线", ["监听账号"], ["task-relay"]),
        (13, "离线", ["监听账号"], ["task-relay"]),
    ]
    assert rows["group:7"].event_backlog_count == 1
    assert rows["group:7"].pending_distribution_count == 1
    assert rows["group:7"].dedup_event_count == 2
    assert rows["group:7"].subscription_event_types == ["源群新消息", "规则分发"]
    assert rows["group:7"].last_event_at == "2026-05-11T10:00:00"
    assert rows["group:7"].recent_events[0].content == "源群事件"
    assert rows["group:7"].backup_account.id == 14
    assert rows["group:7"].switch_recommended is False
    assert "备用账号" in rows["group:7"].switch_reason
    assert rows["group:8"].subscriber_task_count == 1
    assert rows["group:8"].listener_account_count == 1
    assert rows["group:8"].listener_accounts[0].id == 14
    assert rows["group:8"].listener_accounts[0].roles == ["发言账号", "历史采集账号"]
    assert rows["group:8"].subscription_event_types == ["群上下文", "真实用户活跃"]


def test_switch_listener_account_enables_backup_and_disables_offline_listener():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="源群", auth_status="已授权运营", listener_enabled=True, listener_last_error="poll failed"))
        session.add_all(
            [
                TgAccount(id=12, tenant_id=1, display_name="离线监听", username="offline_listener", phone_masked="12", status="离线", health_score=20),
                TgAccount(id=14, tenant_id=1, display_name="备用监听", username="backup_listener", phone_masked="14", status="在线", health_score=95),
                TgGroupAccount(id=71, tenant_id=1, group_id=7, account_id=12, can_send=True, is_listener=True),
                TgGroupAccount(id=72, tenant_id=1, group_id=7, account_id=14, can_send=True, is_listener=False),
                Task(id="task-relay", tenant_id=1, name="转发任务", type="group_relay", status="running", type_config={"source_groups": [{"group_id": 7, "is_active": True}]}),
            ]
        )
        session.commit()

        summary = switch_listener_account(session, 1, "group", 7, 14, "pytest")
        offline_link = session.get(TgGroupAccount, 71)
        backup_link = session.get(TgGroupAccount, 72)
        group = session.get(TgGroup, 7)
        assert offline_link.is_listener is False
        assert backup_link.is_listener is True
        assert group.listener_last_error == ""

    rows = {item.key: item for item in summary.items}
    assert rows["group:7"].listener_accounts[0].id == 14


def test_switch_channel_listener_account_updates_channel_task_accounts():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(OperationTarget(id=21, tenant_id=1, target_type="channel", tg_peer_id="-10021", title="频道", can_send=True, auth_status="已授权运营"))
        session.add_all(
            [
                TgAccount(id=11, tenant_id=1, display_name="离线频道号", username="offline_channel", phone_masked="11", status="离线", health_score=20),
                TgAccount(id=14, tenant_id=1, display_name="备用频道号", username="backup_channel", phone_masked="14", status="在线", health_score=95),
                Task(id="task-channel", tenant_id=1, name="频道点赞", type="channel_like", status="running", account_config={"account_ids": [11]}, type_config={"target_channel_id": 21}),
                Action(id="action-channel", tenant_id=1, task_id="task-channel", task_type="channel_like", action_type="like_message", status="pending"),
            ]
        )
        session.commit()

        summary = switch_listener_account(session, 1, "channel", 21, 14, "pytest")
        task = session.get(Task, "task-channel")
        assert task.account_config["selection_mode"] == "manual"
        assert task.account_config["account_ids"] == [14]

    rows = {item.key: item for item in summary.items}
    assert rows["channel:21"].listener_accounts[0].id == 14
    assert rows["channel:21"].event_backlog_count == 1


def test_listener_center_events_errors_and_watermark_reset_are_audited():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add_all(
            [
                TgGroup(
                    id=7,
                    tenant_id=1,
                    tg_peer_id="-1007",
                    title="源群",
                    auth_status="已授权运营",
                    listener_enabled=True,
                    listener_last_polled_at=datetime(2026, 5, 11, 10, 5, 0),
                    listener_last_error="poll failed",
                ),
                TgAccount(id=12, tenant_id=1, display_name="监听账号A", username="listener_a", phone_masked="12", status="在线", health_score=80),
                TgGroupAccount(id=71, tenant_id=1, group_id=7, account_id=12, can_send=True, is_listener=True),
                GroupContextMessage(
                    id=41,
                    tenant_id=1,
                    group_id=7,
                    listener_account_id=12,
                    sender_peer_id="sender-1",
                    sender_name="普通成员",
                    sender_username="normal_user",
                    sender_role="member",
                    is_bot=False,
                    content="第一条事件",
                    message_type="text",
                    remote_message_id="m1",
                    sent_at=datetime(2026, 5, 11, 10, 0, 0),
                ),
                GroupContextMessage(
                    id=42,
                    tenant_id=1,
                    group_id=7,
                    listener_account_id=12,
                    sender_peer_id="bot-1",
                    sender_name="公告机器人",
                    sender_username="notice_bot",
                    sender_role="admin",
                    is_bot=True,
                    content="第二条事件",
                    message_type="photo",
                    remote_message_id="m2",
                    sent_at=datetime(2026, 5, 11, 10, 3, 0),
                ),
                ListenerSourceState(
                    tenant_id=1,
                    source_type="group",
                    source_peer_id="-1007",
                    account_id=12,
                    shard_key="group:-1007",
                    last_remote_message_id="m2",
                    last_event_at=datetime(2026, 5, 11, 10, 3, 0),
                    backfill_until=datetime(2026, 5, 11, 9, 0, 0),
                    last_error="RPC flood wait",
                ),
                Task(id="task-relay", tenant_id=1, name="转发任务", type="group_relay", status="running", type_config={"source_groups": [{"group_id": 7, "is_active": True}], "monitor_account_ids": [12]}),
            ]
        )
        session.commit()

        events = list_listener_events(session, 1, "group", 7, limit=10)
        errors = list_listener_errors(session, 1, "group", 7)

        with pytest.raises(ValueError, match="请输入确认重置"):
            reset_listener_watermark(session, 1, "group", 7, reason="测试重置", actor="pytest", confirm_text="")

        summary = reset_listener_watermark(session, 1, "group", 7, reason="测试重置监听水位", actor="pytest", confirm_text="确认重置")
        group = session.get(TgGroup, 7)
        state = session.scalar(select(ListenerSourceState).where(ListenerSourceState.source_peer_id == "-1007"))
        audit_log = session.scalar(select(AuditLog).where(AuditLog.action == "重置监听水位").order_by(AuditLog.id.desc()))

    assert [event.remote_message_id for event in events] == ["m2", "m1"]
    assert events[0].sender_peer_id == "bot-1"
    assert events[0].sender_username == "notice_bot"
    assert events[0].sender_role == "admin"
    assert events[0].is_bot is True
    assert errors[0].error_message == "poll failed"
    assert errors[1].error_message == "RPC flood wait"
    assert group.listener_last_polled_at is None
    assert group.listener_last_error == ""
    assert state.last_remote_message_id == ""
    assert state.last_event_at is None
    assert state.backfill_until is None
    assert state.last_error == ""
    assert audit_log.actor == "pytest"
    assert "测试重置监听水位" in audit_log.detail
    rows = {item.key: item for item in summary.items}
    assert rows["group:7"].last_error == ""


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


def test_stale_executing_actions_are_recovered_for_retry_guard():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(
            Task(
                id="stale-executing-task",
                tenant_id=1,
                name="卡住执行项",
                type="group_relay",
                status="running",
                failure_policy={"max_retries": 1, "retry_delay_seconds": 30, "retry_backoff": "none"},
                stats={},
            )
        )
        session.add(
            Action(
                id="stale-action",
                tenant_id=1,
                task_id="stale-executing-task",
                task_type="group_relay",
                action_type="send_message",
                status="executing",
                scheduled_at=datetime(2026, 5, 11, 9, 0, 0),
                lease_owner="worker-a:100",
                lease_expires_at=datetime(2026, 5, 11, 9, 30, 0),
                payload={"chat_id": "-1001", "message_text": "test"},
                result={"telegram_request_id": "req-stale-1"},
            )
        )
        session.commit()

        assert _recover_stale_executing_actions(session, timeout_minutes=30) == 1
        action = session.get(Action, "stale-action")
        task = session.get(Task, "stale-executing-task")

        assert action.status == "failed"
        assert action.result["error_code"] == "execution_timeout"
        assert "投递守护" in action.result["error_message"]
        assert action.result["validation_stage"] == "execution_recovery"
        assert action.result["auto_check"] == "超时恢复"
        assert action.result["recovery_reason"] == "lease_expired"
        assert action.result["previous_lease_owner"] == "worker-a:100"
        assert action.result["previous_lease_expires_at"].startswith("2026-05-11T09:30:00")
        assert action.result["previous_result"]["telegram_request_id"] == "req-stale-1"
        assert action.lease_owner == ""
        assert action.lease_expires_at is None
        assert task.stats["stale_executing_recovered_at"]
        assert task.stats["stale_executing_last_action_id"] == "stale-action"
        assert task.stats["stale_executing_last_lease_owner"] == "worker-a:100"
        assert task.stats["stale_executing_recovered_action_ids"] == ["stale-action"]
        assert task.stats["recovered_execution_timeout_count"] == 1

        assert _retry_failed_actions(session, task) == 1
        assert action.status == "pending"
        assert action.retry_count == 1
        assert action.executed_at is None
        assert action.result["retry_scheduled"] is True
        assert action.result["retry_after_seconds"] == 30
        assert action.result["last_failure"]["error_code"] == "execution_timeout"


def test_stale_worker_heartbeat_recovers_owned_executing_action_before_lease_expiry():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(
            WorkerHeartbeat(
                worker_id="worker-stale:200",
                process_type="task_center",
                hostname="worker-stale",
                pid=200,
                status="active",
                heartbeat_metadata={},
                started_at=now_value - timedelta(minutes=10),
                last_seen_at=now_value - timedelta(minutes=5),
            )
        )
        session.add(
            Task(
                id="stale-worker-task",
                tenant_id=1,
                name="过期 worker 执行项",
                type="group_relay",
                status="running",
                failure_policy={"max_retries": 1, "retry_delay_seconds": 10, "retry_backoff": "none"},
                stats={},
            )
        )
        session.add(
            Action(
                id="stale-worker-action",
                tenant_id=1,
                task_id="stale-worker-task",
                task_type="group_relay",
                action_type="send_message",
                status="executing",
                scheduled_at=now_value,
                lease_owner="worker-stale:200",
                lease_expires_at=now_value + timedelta(minutes=20),
                payload={"chat_id": "-1001", "message_text": "test"},
                result={},
            )
        )
        session.commit()

        assert _recover_stale_executing_actions(session, timeout_minutes=30) == 1
        action = session.get(Action, "stale-worker-action")
        task = session.get(Task, "stale-worker-task")

    assert action.status == "failed"
    assert action.result["recovery_reason"] == "stale_worker"
    assert action.result["previous_lease_owner"] == "worker-stale:200"
    assert task.stats["stale_executing_last_recovery_reason"] == "stale_worker"


def test_task_center_drain_records_worker_heartbeat():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    SessionFactory = sessionmaker(bind=engine, future=True)

    with SessionFactory() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.commit()

    assert drain_task_center(SessionFactory, limit=5) >= 0
    with SessionFactory() as session:
        heartbeat = session.scalar(select(WorkerHeartbeat))
        summary = operation_metrics_summary(session, 1)

    assert heartbeat is not None
    assert heartbeat.process_type == "task_center"
    assert heartbeat.status == "active"
    assert heartbeat.heartbeat_metadata["limit"] == 5
    assert next(item.value for item in summary.risk_control if item.key == "risk.worker_heartbeat") == 1
    assert any(item.category == "进程心跳" and item.related_id == heartbeat.worker_id for item in summary.risk_details)


def test_worker_heartbeat_stale_check_accepts_aware_and_naive_datetimes():
    cutoff = datetime.now(BEIJING_TZ).replace(tzinfo=None) - timedelta(minutes=2)

    assert _is_stale_heartbeat(datetime.now(BEIJING_TZ) - timedelta(minutes=5), cutoff)
    assert not _is_stale_heartbeat(datetime.now(BEIJING_TZ), cutoff)


def test_listener_runtime_collects_shared_sources_once_and_recovers_listener(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    SessionFactory = sessionmaker(bind=engine, future=True)
    reset_listener_runtime_cache()
    seen: list[tuple[int, list[int]]] = []

    def fake_collect(session: Session, group: TgGroup, account_ids: list[int] | None = None, **_kwargs) -> int:
        seen.append((group.id, list(account_ids or [])))
        session.add(
            GroupContextMessage(
                tenant_id=group.tenant_id,
                group_id=group.id,
                listener_account_id=(account_ids or [101])[0],
                sender_peer_id="real-user",
                sender_name="真实用户",
                content="监听运行层采集到的新消息",
                message_type="text",
                remote_message_id=f"runtime-{len(seen)}",
                sent_at=datetime(2026, 5, 11, 10, 0, 0),
            )
        )
        session.flush()
        return 1

    with SessionFactory() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        future_run = _now() + timedelta(hours=6)
        session.add_all(
            [
                TgAccount(id=101, tenant_id=1, display_name="备用监听号", phone_masked="101", status="在线", health_score=90),
                TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="源群", auth_status="已授权运营", listener_interval_seconds=60, listener_last_error="上一轮监听失败"),
                TgGroup(id=9, tenant_id=1, tg_peer_id="-1009", title="目标群", auth_status="已授权运营"),
                OperationTarget(id=21, tenant_id=1, target_type="group", tg_peer_id="-1007", title="源群目标", can_send=True, auth_status="已授权运营"),
                OperationTarget(id=22, tenant_id=1, target_type="group", tg_peer_id="-1009", title="目标群目标", can_send=True, auth_status="已授权运营"),
                TgGroupAccount(id=701, tenant_id=1, group_id=7, account_id=101, can_send=True, is_listener=False),
                TgGroupAccount(id=901, tenant_id=1, group_id=9, account_id=101, can_send=True, is_listener=False),
                Task(
                    id="runtime-relay",
                    tenant_id=1,
                    name="共享源群转发",
                    type="group_relay",
                    status="running",
                    next_run_at=future_run,
                    stats={"listener_runtime_last_error": "没有可用监听账号"},
                    type_config={"source_groups": [{"operation_target_id": 21, "is_active": True}], "target_operation_target_id": 22, "monitor_account_ids": []},
                ),
                Task(
                    id="runtime-ai",
                    tenant_id=1,
                    name="共享源群 AI",
                    type="group_ai_chat",
                    status="running",
                    next_run_at=future_run,
                    account_config={"selection_mode": "manual", "account_ids": [101], "max_concurrent": 1, "cooldown_per_account_minutes": 0},
                    type_config={"target_operation_target_id": 21, "chat_history_depth": 20},
                    stats={"listener_runtime_last_error": "没有可用监听账号"},
                ),
            ]
        )
        session.commit()

    monkeypatch.setattr("app.services.task_center.listener_runtime.collect_group_context", fake_collect)
    result = drain_listener_runtime(SessionFactory, tenant_id=1, limit=10)

    with SessionFactory() as session:
        link = session.get(TgGroupAccount, 701)
        group = session.get(TgGroup, 7)
        relay_task = session.get(Task, "runtime-relay")
        ai_task = session.get(Task, "runtime-ai")
        context_count = session.scalar(select(func.count(GroupContextMessage.id)))
        audit_count = session.scalar(select(func.count(AuditLog.id)).where(AuditLog.action == "自动恢复监听账号"))

    assert seen == [(7, [101])]
    assert result.source_count == 1
    assert result.collected_count == 1
    assert result.recovered_count == 1
    assert link.is_listener is True
    assert group.listener_enabled is True
    assert group.listener_last_error == ""
    assert context_count == 1
    assert audit_count == 1
    assert relay_task.stats["listener_runtime_last_collect_count"] == 1
    assert "listener_runtime_last_error" not in relay_task.stats
    assert ai_task.stats["listener_runtime_last_source_group_id"] == 7
    assert "listener_runtime_last_error" not in ai_task.stats
    assert relay_task.next_run_at < future_run
    assert ai_task.next_run_at < future_run


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


def test_task_center_group_policy_ignores_hidden_group_rate_limits():
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
            daily_limit=1,
            group_cooldown_seconds=3600,
            banned_words="",
        )
        task = Task(id="task-ai", tenant_id=1, name="AI 活跃群", type="group_ai_chat", status="running")
        session.add_all([group, task])
        session.add(
            Action(
                tenant_id=1,
                task_id=task.id,
                task_type=task.type,
                action_type="send_message",
                status="success",
                executed_at=datetime.now(UTC).replace(tzinfo=None),
                payload={"group_id": group.id},
            )
        )
        session.commit()

        assert validate_group_send_policy(
            session,
            tenant_id=1,
            group=group,
            content="任务中心只受全局风控和任务每小时上限约束",
            review_approved=True,
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


def test_group_relay_filter_expression_supports_all_and_any_conditions():
    filters = {
        "expression": {
            "mode": "all",
            "conditions": [
                {"field": "content", "operator": "contains", "value": ["公告", "活动"]},
                {"field": "content", "operator": "not_contains", "value": ["禁止"]},
                {"field": "message_type", "operator": "in", "value": ["text"]},
                {"field": "length", "operator": "gte", "value": 4},
            ],
        }
    }

    assert passes_relay_filters("活动公告", "1001", "text", filters) is True
    assert passes_relay_filters("禁止活动公告", "1001", "text", filters) is False
    assert passes_relay_filters("活动公告", "1001", "photo", filters) is False

    any_filters = {
        "expression": {
            "mode": "any",
            "conditions": [
                {"field": "sender_id", "operator": "eq", "value": "42"},
                {"field": "content", "operator": "contains", "value": "紧急"},
            ],
        }
    }

    assert passes_relay_filters("普通消息", "42", "text", any_filters) is True
    assert passes_relay_filters("紧急消息", "1001", "text", any_filters) is True
    assert passes_relay_filters("普通消息", "1001", "text", any_filters) is False

    nested_filters = {
        "expression": {
            "mode": "all",
            "conditions": [
                {"field": "message_type", "operator": "eq", "value": "text"},
                {
                    "mode": "any",
                    "conditions": [
                        {"field": "content", "operator": "contains", "value": "报名"},
                        {"field": "sender_id", "operator": "eq", "value": "vip-user"},
                    ],
                },
            ],
        }
    }

    assert passes_relay_filters("报名开始", "normal-user", "text", nested_filters) is True
    assert passes_relay_filters("普通消息", "vip-user", "text", nested_filters) is True
    assert passes_relay_filters("普通消息", "normal-user", "text", nested_filters) is False
    assert passes_relay_filters("报名开始", "normal-user", "photo", nested_filters) is False


def test_group_tasks_accept_operation_target_ids_as_primary_references():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add_all(
            [
                TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="源群", auth_status="已授权运营", can_send=True),
                TgGroup(id=9, tenant_id=1, tg_peer_id="-1009", title="目标群", auth_status="已授权运营", can_send=True),
                OperationTarget(id=21, tenant_id=1, target_type="group", tg_peer_id="-1007", title="源群目标", can_send=True, auth_status="已授权运营"),
                OperationTarget(id=22, tenant_id=1, target_type="group", tg_peer_id="-1009", title="目标群目标", can_send=True, auth_status="已授权运营"),
                PromptTemplate(id=31, tenant_id=None, template_type="AI黑话词表", name="默认黑话", content="老师=妓女", is_active=True),
                RuleSet(id=41, tenant_id=1, name="历史规则集", status="active", active_version_id=42),
                RuleSetVersion(id=42, tenant_id=1, rule_set_id=41, version=1, status="archived"),
            ]
        )
        session.commit()

        ai_task = create_group_ai_chat_task(
            session,
            1,
            GroupAIChatTaskCreate(name="运营目标 AI 活跃", target_operation_target_id=21),
            "tester",
        )
        assert ai_task.type_config["target_operation_target_id"] == 21
        assert ai_task.type_config["target_group_id"] == 7
        assert ai_task.type_config["target_group_name"] == "源群目标"
        assert ai_task.type_config["slang_prompt_template_id"] == 31
        assert ai_task.pacing_config["operation_profile"]["template_id"] == "natural_full_day"
        assert len(ai_task.pacing_config["operation_profile"]["hourly_activity_curve"]) == 24
        assert "silent_start" not in ai_task.type_config
        assert "ramp_up_minutes" not in ai_task.type_config
        assert "jitter_percent" not in ai_task.pacing_config

        relay_task = create_group_relay_task(
            session,
            1,
            GroupRelayTaskCreate(
                name="运营目标转发",
                source_groups=[{"operation_target_id": 21}],
                target_operation_target_id=22,
                target_operation_target_ids=[22],
            ),
            "tester",
        )
        assert relay_task.type_config["source_groups"] == [{"group_id": 7, "operation_target_id": 21, "group_name": "源群目标", "is_active": True}]
        assert relay_task.type_config["target_operation_target_id"] == 22
        assert relay_task.type_config["target_group_id"] == 9
        assert relay_task.type_config["target_group_ids"] == [9]

        manual_curve = [0, 0, 0, 0, 0, 0, 2, 4, 8, 12, 16, 20, 16, 12, 8, 6, 10, 14, 18, 20, 16, 10, 4, 2]
        custom_task = create_group_ai_chat_task(
            session,
            1,
            GroupAIChatTaskCreate(
                name="手动曲线 AI 活跃",
                target_operation_target_id=21,
                pacing_config={"operation_profile": {"template_id": "event_warmup", "source": "manual", "hourly_activity_curve": manual_curve, "manual_override": True}},
            ),
            "tester",
        )
        assert custom_task.pacing_config["operation_profile"]["source"] == "manual"
        assert custom_task.pacing_config["operation_profile"]["hourly_activity_curve"] == manual_curve

        with pytest.raises(ValueError, match="只能绑定已发布规则版本"):
            create_group_ai_chat_task(
                session,
                1,
                GroupAIChatTaskCreate(name="历史规则 AI 活跃", target_operation_target_id=21, rule_set_id=41, rule_set_version_id=42),
                "tester",
            )


def test_task_creation_precheck_covers_group_and_channel_requirements():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with pytest.raises(ValueError):
        TaskSettingsUpdate(target_input="@edit_should_not_create")

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add_all(
            [
                TgAccount(id=11, tenant_id=1, display_name="在线号A", phone_masked="11", status=AccountStatus.ACTIVE.value, health_score=95),
                TgAccount(id=12, tenant_id=1, display_name="在线号B", phone_masked="12", status=AccountStatus.ACTIVE.value, health_score=90),
                TgAccount(id=13, tenant_id=1, display_name="受限号", phone_masked="13", status=AccountStatus.LIMITED.value, health_score=30),
                TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="目标群", auth_status="已授权运营", can_send=True),
                TgGroup(id=8, tenant_id=1, tg_peer_id="-1008", title="只监听源群", auth_status="已授权运营", can_send=False),
                OperationTarget(id=21, tenant_id=1, target_type="group", tg_peer_id="-1007", title="目标群运营对象", can_send=True, auth_status="已授权运营"),
                OperationTarget(id=22, tenant_id=1, target_type="group", tg_peer_id="-1008", title="只监听源群运营对象", can_send=False, auth_status="已授权运营"),
                OperationTarget(id=31, tenant_id=1, target_type="channel", tg_peer_id="-2001", title="频道运营对象", can_send=True, auth_status="已授权运营"),
                ChannelMessage(id=41, tenant_id=1, channel_target_id=31, message_id=1001, content_preview="频道消息"),
                RuleSet(id=51, tenant_id=1, name="发布规则集", status="active", active_version_id=52),
                RuleSetVersion(id=52, tenant_id=1, rule_set_id=51, version=3, status="published"),
            ]
        )
        session.commit()

        ai_result = precheck_task_creation(
            session,
            1,
            TaskPrecheckRequest(
                task_type="group_ai_chat",
                payload={
                    "name": "AI 活跃预检",
                    "target_operation_target_id": 21,
                    "rule_set_id": 51,
                    "rule_set_version_id": 52,
                    "account_config": {"selection_mode": "manual", "account_ids": [11, 12, 13], "max_concurrent": 3, "cooldown_per_account_minutes": 0},
                    "messages_per_round_mode": "manual",
                    "messages_per_round": 2,
                },
            ),
        )
        assert ai_result["decision"] in {"allow", "warn"}
        assert ai_result["candidate_account_count"] == 3
        assert ai_result["available_account_count"] == 2
        assert ai_result["limited_account_count"] + ai_result["blocked_account_count"] >= 1
        assert ai_result["estimated_actions"] == 2
        assert ai_result["target_ability"][0]["can_task"] is True
        assert ai_result["rule_version"] == {"id": 52, "rule_set_id": 51, "version": 3, "status": "published"}

        channel_result = precheck_task_creation(
            session,
            1,
            TaskPrecheckRequest(
                task_type="channel_like",
                payload={
                    "name": "频道点赞预检",
                    "target_channel_id": 31,
                    "message_scope": "specific",
                    "message_ids": [41],
                    "target_likes_per_message": 5,
                    "account_config": {"selection_mode": "manual", "account_ids": [11, 12], "max_concurrent": 2, "cooldown_per_account_minutes": 0},
                },
            ),
        )
        assert channel_result["decision"] == "warn"
        assert channel_result["estimated_actions"] == 5
        assert channel_result["capacity_shortfall"] == 3
        assert channel_result["target_ability"][0]["target_type"] == "channel"

        relay_result = precheck_task_creation(
            session,
            1,
            TaskPrecheckRequest(
                task_type="group_relay",
                payload={
                    "name": "转发预检",
                    "source_groups": [{"operation_target_id": 22}],
                    "target_operation_target_id": 21,
                    "target_operation_target_ids": [21],
                    "account_config": {"selection_mode": "manual", "account_ids": [11, 12], "max_concurrent": 2, "cooldown_per_account_minutes": 0},
                },
            ),
        )
        assert relay_result["estimated_actions"] == 1
        assert all(item["can_task"] for item in relay_result["target_ability"])
        assert any(item["role"] == "listen_source" and item["can_send"] is False for item in relay_result["target_ability"])
        assert relay_result["target_resolution"]["sources"][0]["target_id"] == 22
        assert relay_result["target_resolution"]["targets"][0]["target_id"] == 21
        assert relay_result["membership_summary"]["target_count"] == 2
        assert relay_result["estimated_membership_actions"] == 4
        assert relay_result["membership_subtask_preview"]["pending_account_count"] == 4

        view_result = precheck_task_creation(
            session,
            1,
            TaskPrecheckRequest(
                task_type="channel_view",
                payload={
                    "name": "浏览预检",
                    "target_channel_id": 31,
                    "message_scope": "specific",
                    "message_ids": [41],
                    "target_views_per_message": 2,
                    "account_config": {"selection_mode": "manual", "account_ids": [11, 12], "max_concurrent": 2, "cooldown_per_account_minutes": 0},
                },
            ),
        )
        assert view_result["estimated_actions"] == 2
        assert view_result["capacity_shortfall"] == 0

        comment_result = precheck_task_creation(
            session,
            1,
            TaskPrecheckRequest(
                task_type="channel_comment",
                payload={
                    "name": "评论预检",
                    "target_channel_id": 31,
                    "message_scope": "specific",
                    "message_ids": [41],
                    "target_comments_per_message": 1,
                    "rule_set_id": 51,
                    "rule_set_version_id": 52,
                    "account_config": {"selection_mode": "manual", "account_ids": [11], "max_concurrent": 1, "cooldown_per_account_minutes": 0},
                },
            ),
        )
        assert comment_result["estimated_actions"] == 1
        assert comment_result["rule_version"]["id"] == 52

        blocked = precheck_task_creation(
            session,
            1,
            TaskPrecheckRequest(
                task_type="group_ai_chat",
                payload={
                    "name": "阻塞预检",
                    "target_operation_target_id": 999,
                    "account_config": {"selection_mode": "manual", "account_ids": [11], "cooldown_per_account_minutes": 0},
                },
            ),
        )
        assert blocked["decision"] == "block"
        assert blocked["blockers"]


def test_group_ai_precheck_warns_for_preparable_target_and_mixed_account_health():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add_all(
            [
                TgAccount(id=11, tenant_id=1, display_name="在线号", phone_masked="11", status=AccountStatus.ACTIVE.value, health_score=95),
                TgAccount(id=12, tenant_id=1, display_name="受限号", phone_masked="12", status=AccountStatus.LIMITED.value, health_score=30),
                OperationTarget(
                    id=21,
                    tenant_id=1,
                    target_type="group",
                    tg_peer_id="-10021",
                    title="待准入目标群",
                    username="joinable_group",
                    can_send=False,
                    auth_status="只读归档",
                ),
            ]
        )
        session.commit()

        result = precheck_task_creation(
            session,
            1,
            TaskPrecheckRequest(
                task_type="group_ai_chat",
                payload={
                    "name": "可补齐准入 AI 活跃",
                    "target_operation_target_id": 21,
                    "account_config": {"selection_mode": "manual", "account_ids": [11, 12], "cooldown_per_account_minutes": 0},
                },
            ),
        )

    assert result["target_ability"][0]["can_task"] is True
    assert result["available_account_count"] == 1
    assert result["decision"] == "warn"
    assert result["blockers"] == []
    assert "target_warning" in result["warnings"]
    assert "account_blocked" in result["warnings"]


def test_task_settings_update_normalizes_operation_target_references():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add_all(
            [
                TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="源群资产", auth_status="已授权运营", can_send=True),
                TgGroup(id=9, tenant_id=1, tg_peer_id="-1009", title="目标群资产", auth_status="已授权运营", can_send=True),
                OperationTarget(id=21, tenant_id=1, target_type="group", tg_peer_id="-1007", title="源群目标", can_send=True, auth_status="已授权运营"),
                OperationTarget(id=22, tenant_id=1, target_type="group", tg_peer_id="-1009", title="目标群目标", can_send=True, auth_status="已授权运营"),
                Task(id="ai-settings", tenant_id=1, name="待编辑 AI", type="group_ai_chat", status="running", type_config={"target_group_id": 7, "target_group_name": "旧的缓存群名", "silent_start": "23:00", "ramp_up_minutes": 60}),
                Task(id="relay-settings", tenant_id=1, name="待编辑转发", type="group_relay", status="running", type_config={"source_groups": [{"group_id": 7}], "target_group_id": 7, "target_group_ids": [7]}),
            ]
        )
        session.commit()

        ai_task = update_task_settings(
            session,
            1,
            "ai-settings",
            TaskSettingsUpdate(target_operation_target_id=22),
            "pytest",
        )
        task = update_task_settings(
            session,
            1,
            "relay-settings",
            TaskSettingsUpdate(
                source_groups=[{"operation_target_id": 21}],
                target_operation_target_id=22,
                target_operation_target_ids=[22],
            ),
            "pytest",
        )
        ai_config = dict(ai_task.type_config)
        relay_config = dict(task.type_config)

    assert ai_config["target_operation_target_id"] == 22
    assert ai_config["target_group_id"] == 9
    assert ai_config["target_group_name"] == "目标群目标"
    assert "silent_start" not in ai_config
    assert "ramp_up_minutes" not in ai_config
    assert relay_config["source_groups"] == [{"group_id": 7, "operation_target_id": 21, "group_name": "源群目标", "is_active": True}]
    assert relay_config["target_operation_target_id"] == 22
    assert relay_config["target_group_id"] == 9
    assert relay_config["target_group_ids"] == [9, 7]


def test_task_settings_update_accepts_group_ai_chat_quality_fields():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add_all(
            [
                TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="目标群", auth_status="已授权运营", can_send=True),
                OperationTarget(id=21, tenant_id=1, target_type="group", tg_peer_id="-1007", title="目标群", can_send=True, auth_status="已授权运营"),
                Task(id="ai-quality-settings", tenant_id=1, name="郑州锦鲤", type="group_ai_chat", status="running", type_config={"target_group_id": 7}),
            ]
        )
        session.commit()

        task = update_task_settings(
            session,
            1,
            "ai-quality-settings",
            TaskSettingsUpdate(
                slang_prompt_template_id=31,
                slang_terms={"老师": "特殊称呼"},
                account_memory_depth=7,
                context_expire_after_messages=25,
            ),
            "pytest",
        )
        config = dict(task.type_config)

    assert config["slang_prompt_template_id"] == 31
    assert config["slang_terms"] == {"老师": "特殊称呼"}
    assert config["account_memory_depth"] == 7
    assert config["context_expire_after_messages"] == 25


def test_group_executors_resolve_operation_targets_without_normalized_group_ids(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.should_collect_listener", lambda *_args, **_kwargs: False)
    monkeypatch.setattr("app.services.task_center.executors.group_relay.should_collect_listener", lambda *_args, **_kwargs: False)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.generate_group_messages", lambda *_args, **_kwargs: (["运营目标直连发言"], 0))
    monkeypatch.setattr("app.services.task_center.executors.group_relay.rewrite_relay_content", lambda *_args, **_kwargs: ("转发内容", 0))

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add_all(
            [
                TgAccount(id=101, tenant_id=1, display_name="账号A", phone_masked="101", status=AccountStatus.ACTIVE.value, health_score=100),
                TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="源群资产", auth_status="已授权运营", can_send=True, listener_context_limit=20),
                TgGroup(id=9, tenant_id=1, tg_peer_id="-1009", title="目标群资产", auth_status="已授权运营", can_send=True, listener_context_limit=20),
                OperationTarget(id=21, tenant_id=1, target_type="group", tg_peer_id="-1007", title="源群目标", can_send=True, auth_status="已授权运营"),
                OperationTarget(id=22, tenant_id=1, target_type="group", tg_peer_id="-1009", title="目标群目标", can_send=True, auth_status="已授权运营"),
                TgGroupAccount(id=901, tenant_id=1, group_id=7, account_id=101, can_send=True, is_listener=True),
                TgGroupAccount(id=902, tenant_id=1, group_id=9, account_id=101, can_send=True, is_listener=True),
                GroupContextMessage(
                    id=41,
                    tenant_id=1,
                    group_id=7,
                    listener_account_id=101,
                    sender_peer_id="user-1",
                    sender_name="真实用户",
                    content="运营目标直连上下文",
                    remote_message_id="op-direct-ctx",
                    sent_at=datetime(2026, 5, 11, 10, 0, 0),
                ),
                Task(
                    id="ai-op-only-runtime",
                    tenant_id=1,
                    name="AI 运营目标直连",
                    type="group_ai_chat",
                    status="running",
                    account_config={"selection_mode": "manual", "account_ids": [101], "max_concurrent": 1, "cooldown_per_account_minutes": 0},
                    pacing_config={"mode": "fixed", "interval_seconds_min": 0, "interval_seconds_max": 0, "jitter_percent": 0},
                    type_config={
                        "target_operation_target_id": 21,
                        "messages_per_round_mode": "manual",
                        "messages_per_round": 1,
                        "silent_mode_enabled": False,
                    },
                ),
                Task(
                    id="relay-op-only-runtime",
                    tenant_id=1,
                    name="转发运营目标直连",
                    type="group_relay",
                    status="running",
                    account_config={"selection_mode": "manual", "account_ids": [101], "max_concurrent": 1, "cooldown_per_account_minutes": 0},
                    pacing_config={"mode": "fixed", "interval_seconds_min": 0, "interval_seconds_max": 0, "jitter_percent": 0},
                    type_config={
                        "source_groups": [{"operation_target_id": 21, "is_active": True}],
                        "target_operation_target_ids": [22],
                        "content_mode": "raw",
                        "dedup_window_minutes": 60,
                    },
                ),
            ]
        )
        session.commit()

        assert build_group_ai_chat_plan(session, session.get(Task, "ai-op-only-runtime")) == 1
        assert build_group_relay_plan(session, session.get(Task, "relay-op-only-runtime")) == 1
        ai_action = session.scalar(select(Action).where(Action.task_id == "ai-op-only-runtime", Action.action_type == "send_message"))
        relay_action = session.scalar(select(Action).where(Action.task_id == "relay-op-only-runtime", Action.action_type == "send_message"))
        ai_detail = get_task_detail(session, 1, "ai-op-only-runtime")
        relay_detail = get_task_detail(session, 1, "relay-op-only-runtime")

    assert ai_action.payload["group_id"] == 7
    assert ai_action.payload["operation_target_id"] == 21
    assert ai_detail["task"]["target_summary"] == "源群目标"
    assert relay_action.payload["group_id"] == 9
    assert relay_action.payload["operation_target_id"] == 22
    assert relay_action.payload["source_group_id"] == 7
    assert relay_action.payload["source_operation_target_id"] == 21
    assert "源群目标" in relay_detail["task"]["target_summary"]
    assert "目标群目标" in relay_detail["task"]["target_summary"]


def test_group_relay_operation_targets_create_membership_precondition(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    from app.integrations.telegram import ChannelMembershipResult
    from app.services.task_center import dispatcher
    from app.services.task_center.dispatcher import dispatch_action

    monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        dispatcher.gateway,
        "ensure_channel_membership",
        lambda *args, **kwargs: ChannelMembershipResult(True, detail="joined", membership_status="joined"),
    )
    monkeypatch.setattr("app.services.task_center.executors.group_relay.should_collect_listener", lambda *_args, **_kwargs: False)
    monkeypatch.setattr("app.services.task_center.executors.group_relay.rewrite_relay_content", lambda *_args, **_kwargs: ("转发内容", 0))

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add_all(
            [
                TgAccount(id=101, tenant_id=1, display_name="账号A", phone_masked="101", status=AccountStatus.ACTIVE.value, health_score=100),
                TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="源群资产", auth_status="已授权运营", can_send=True, listener_context_limit=20),
                TgGroup(id=9, tenant_id=1, tg_peer_id="-1009", title="目标群资产", auth_status="未确认", can_send=False, listener_context_limit=20),
                OperationTarget(id=21, tenant_id=1, target_type="group", tg_peer_id="-1007", title="源群目标", can_send=True, auth_status="已授权运营"),
                OperationTarget(id=22, tenant_id=1, target_type="group", tg_peer_id="-1009", title="待加入目标群", can_send=False, auth_status="未确认"),
                TgGroupAccount(id=901, tenant_id=1, group_id=7, account_id=101, can_send=True, is_listener=True),
                TgGroupAccount(id=902, tenant_id=1, group_id=9, account_id=101, can_send=False, is_listener=False),
                GroupContextMessage(
                    id=41,
                    tenant_id=1,
                    group_id=7,
                    listener_account_id=101,
                    sender_peer_id="user-1",
                    sender_name="真实用户",
                    content="需要转发的上下文",
                    remote_message_id="relay-membership-ctx",
                    sent_at=datetime(2026, 5, 11, 10, 0, 0),
                ),
                Task(
                    id="relay-target-membership",
                    tenant_id=1,
                    name="转发目标准入",
                    type="group_relay",
                    status="running",
                    account_config={"selection_mode": "manual", "account_ids": [101], "max_concurrent": 1, "cooldown_per_account_minutes": 0},
                    pacing_config={"mode": "fixed", "interval_seconds_min": 0, "interval_seconds_max": 0, "jitter_percent": 0},
                    type_config={
                        "source_groups": [{"operation_target_id": 21, "is_active": True}],
                        "target_operation_target_ids": [22],
                        "content_mode": "raw",
                        "dedup_window_minutes": 60,
                    },
                    stats={},
                ),
            ]
        )
        session.commit()

        assert build_group_relay_plan(session, session.get(Task, "relay-target-membership")) == 2
        membership_actions = list(session.scalars(select(Action).where(Action.task_id == "relay-target-membership", Action.action_type == "ensure_target_membership").order_by(Action.payload["channel_target_id"].as_integer())))
        assert [action.payload["channel_target_id"] for action in membership_actions] == [21, 22]
        assert [action.payload["target_type"] for action in membership_actions] == ["group", "group"]
        assert [action.payload["require_send"] for action in membership_actions] == [False, True]
        assert [(action.payload["channel_target_id"], action.status) for action in membership_actions] == [(21, "skipped"), (22, "pending")]
        assert session.scalar(select(func.count(Action.id)).where(Action.task_id == "relay-target-membership", Action.action_type == "send_message")) == 0
        dispatch_action(session, membership_actions[1])
        assert build_group_relay_plan(session, session.get(Task, "relay-target-membership")) == 1
        assert session.scalar(select(func.count(Action.id)).where(Action.task_id == "relay-target-membership", Action.action_type == "send_message")) == 1


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
                OperationTarget(id=21, tenant_id=1, target_type="group", tg_peer_id="-1007", title="源群目标", can_send=True, auth_status="已授权运营"),
                OperationTarget(id=22, tenant_id=1, target_type="group", tg_peer_id="-1009", title="目标群目标", can_send=True, auth_status="已授权运营"),
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
                        "source_groups": [{"group_id": 7, "operation_target_id": 21, "is_active": True}],
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
        action.retry_count = 2
        session.flush()
        detail = get_task_detail(session, 1, "relay-strategy")
        attribution_csv = relay_attribution_csv(session, 1)
        attribution_report = relay_attribution_report(session, 1)
        account_id = action.account_id
        payload = dict(action.payload)

    assert account_id == 102
    assert payload["source_operation_target_id"] == 21
    assert payload["operation_target_id"] == 22
    assert payload["source_group_title"] == "源群"
    assert payload["source_sender_name"] == "用户"
    assert payload["source_sender_peer_id"] == "user-1"
    assert payload["source_remote_message_id"] == "src-1"
    assert payload["source_message_type"] == "text"
    assert payload["source_sent_at"].startswith("2026-05-11T10:00:00")
    assert payload["rule_set_name"] == "固定账号规则"
    assert payload["rule_set_version"] == 1
    assert "白名单 公告" in payload["rule_trace"]["summary"]
    assert payload["rule_trace"]["routing"] == "默认路由->9"
    assert payload["rule_trace"]["account_strategy"] == {"mode": "fixed", "account_id": 102}
    assert detail["relay_batches"][0]["source_event_count"] == 1
    assert detail["relay_batches"][0]["material_count"] == 1
    assert detail["relay_batches"][0]["rule_version_count"] == 1
    assert detail["relay_batches"][0]["items"][0]["retry_count"] == 2
    assert detail["relay_batches"][0]["items"][0]["source_event_key"] == f"21:{payload['relay_event_id']}"
    assert detail["relay_batches"][0]["items"][0]["source_group_title"] == "源群"
    assert detail["relay_batches"][0]["items"][0]["source_sender_name"] == "用户"
    assert detail["relay_batches"][0]["items"][0]["source_remote_message_id"] == "src-1"
    assert detail["relay_batches"][0]["items"][0]["target_display"] == "目标群"
    assert detail["relay_batches"][0]["items"][0]["rule_set_name"] == "固定账号规则"
    assert detail["relay_batches"][0]["items"][0]["rule_set_version"] == 1
    assert detail["relay_batches"][0]["items"][0]["material_fingerprint"] == content_fingerprint("公告：今晚活动开始")
    assert detail["relay_batches"][0]["items"][0]["rule_trace"]["filters"] == ["白名单 公告"]
    assert "relay_batch_id,relay_event_id,source_event_key" in attribution_csv
    assert payload["relay_event_id"] in attribution_csv
    assert content_fingerprint("公告：今晚活动开始") in attribution_csv
    assert attribution_report.total_materials == 1
    assert attribution_report.total_actions == 1
    assert attribution_report.rows[0].material_fingerprint == content_fingerprint("公告：今晚活动开始")
    assert attribution_report.rows[0].retry_count == 2


def test_group_relay_source_filter_defaults_and_allows_bot_when_disabled(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    monkeypatch.setattr("app.services.task_center.executors.group_relay.should_collect_listener", lambda *_args, **_kwargs: False)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add_all(
            [
                TgAccount(
                    id=101,
                    tenant_id=1,
                    display_name="发送号",
                    phone_masked="101",
                    status=AccountStatus.ACTIVE.value,
                    health_score=100,
                    session_ciphertext="session-101",
                ),
                TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="源群", auth_status="已授权运营", listener_context_limit=20),
                TgGroup(id=9, tenant_id=1, tg_peer_id="-1009", title="目标群", auth_status="已授权运营", can_send=True, listener_context_limit=20),
                TgGroupAccount(id=901, tenant_id=1, group_id=9, account_id=101, can_send=True),
                GroupContextMessage(
                    id=41,
                    tenant_id=1,
                    group_id=7,
                    listener_account_id=101,
                    sender_peer_id="bot-1",
                    sender_name="公告机器人",
                    sender_username="notice_bot",
                    is_bot=True,
                    sender_role="member",
                    content="机器人公告：今晚活动开始",
                    remote_message_id="bot-msg-1",
                    sent_at=datetime(2026, 5, 17, 10, 0, 0),
                ),
                Task(
                    id="relay-default-bot-filter",
                    tenant_id=1,
                    name="默认屏蔽机器人",
                    type="group_relay",
                    status="running",
                    account_config={"selection_mode": "manual", "account_ids": [101], "max_concurrent": 1, "cooldown_per_account_minutes": 0},
                    pacing_config={"mode": "fixed", "interval_seconds_min": 0, "interval_seconds_max": 0, "jitter_percent": 0},
                    type_config={"source_groups": [{"group_id": 7, "is_active": True}], "target_group_id": 9, "content_mode": "raw", "dedup_window_minutes": 60},
                ),
                Task(
                    id="relay-allow-bot",
                    tenant_id=1,
                    name="允许机器人",
                    type="group_relay",
                    status="running",
                    account_config={"selection_mode": "manual", "account_ids": [101], "max_concurrent": 1, "cooldown_per_account_minutes": 0},
                    pacing_config={"mode": "fixed", "interval_seconds_min": 0, "interval_seconds_max": 0, "jitter_percent": 0},
                    type_config={"source_groups": [{"group_id": 7, "is_active": True}], "target_group_id": 9, "content_mode": "raw", "dedup_window_minutes": 60, "filter_bot_messages": False},
                ),
            ]
        )
        session.commit()

        assert build_group_relay_plan(session, session.get(Task, "relay-default-bot-filter")) == 0
        assert session.scalar(select(func.count(Action.id)).where(Action.task_id == "relay-default-bot-filter")) == 0
        default_detail = get_task_detail(session, 1, "relay-default-bot-filter")
        assert default_detail["recent_relay_sources"][0]["source_filter_reason"] == "屏蔽机器人消息"
        updated = update_task_settings(session, 1, "relay-default-bot-filter", TaskSettingsUpdate(excluded_sender_peer_ids=["bot-1"]), "pytest")
        assert updated.type_config["excluded_sender_peer_ids"] == ["bot-1"]
        assert build_group_relay_plan(session, session.get(Task, "relay-allow-bot")) == 1
        action = session.scalar(select(Action).where(Action.task_id == "relay-allow-bot"))
        detail = get_task_detail(session, 1, "relay-allow-bot")

    assert action.payload["source_is_bot"] is True
    assert action.payload["source_sender_username"] == "notice_bot"
    assert detail["relay_batches"][0]["items"][0]["source_is_bot"] is True
    assert detail["relay_batches"][0]["items"][0]["source_sender_username"] == "notice_bot"
    assert detail["recent_relay_sources"][0]["is_bot"] is True


def test_group_relay_source_filter_blocks_admin_and_excluded_senders():
    admin_message = SimpleNamespace(is_bot=False, sender_role="admin", sender_peer_id="admin-1", sender_username="admin_user", sender_name="群管理员")
    peer_message = SimpleNamespace(is_bot=False, sender_role="member", sender_peer_id="user-1", sender_username="user_one", sender_name="用户一")
    username_message = SimpleNamespace(is_bot=False, sender_role="member", sender_peer_id="user-2", sender_username="target_user", sender_name="用户二")
    name_message = SimpleNamespace(is_bot=False, sender_role="member", sender_peer_id="user-3", sender_username="", sender_name="同名用户")

    assert relay_source_filter_reason(admin_message, {"filter_admin_messages": True}) == "不转发群主和管理员消息"
    assert relay_source_filter_reason(peer_message, {"excluded_sender_peer_ids": ["user-1"]}) == "命中来源不转发名单：sender_peer_id"
    assert relay_source_filter_reason(username_message, {"excluded_sender_usernames": ["@target_user"]}) == "命中来源不转发名单：@username"
    assert relay_source_filter_reason(name_message, {"excluded_sender_names": ["同名用户"]}) == "昵称兜底命中来源不转发名单"


def test_group_relay_source_filter_override_is_task_local_and_audited():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add_all(
            [
                TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="源群", auth_status="已授权运营", listener_context_limit=20),
                TgGroup(id=9, tenant_id=1, tg_peer_id="-1009", title="目标群", auth_status="已授权运营", listener_context_limit=20),
                RuleSet(id=41, tenant_id=1, name="转发规则", task_types=["group_relay"], active_version_id=42),
                RuleSetVersion(
                    id=42,
                    tenant_id=1,
                    rule_set_id=41,
                    version=1,
                    status="published",
                    filters={"keyword_whitelist": ["公告"]},
                    version_note="已发布版本不应被任务覆盖改写",
                ),
                Task(
                    id="relay-source-override",
                    tenant_id=1,
                    name="来源覆盖",
                    type="group_relay",
                    status="running",
                    type_config={
                        "source_groups": [{"group_id": 7, "is_active": True}],
                        "target_group_id": 9,
                        "target_group_ids": [9],
                        "rule_set_id": 41,
                        "rule_set_version_id": 42,
                        "excluded_sender_peer_ids": ["old-peer"],
                        "excluded_sender_usernames": ["old_user"],
                    },
                ),
                Task(
                    id="relay-other-task",
                    tenant_id=1,
                    name="另一个任务",
                    type="group_relay",
                    status="running",
                    type_config={
                        "source_groups": [{"group_id": 7, "is_active": True}],
                        "target_group_id": 9,
                        "target_group_ids": [9],
                        "rule_set_id": 41,
                        "rule_set_version_id": 42,
                    },
                ),
            ]
        )
        session.commit()

        updated = add_task_source_filter_override(
            session,
            1,
            "relay-source-override",
            TaskSourceFilterOverrideRequest(
                sender_peer_id="new-peer",
                sender_username="@new_user",
                sender_name="新来源",
                source_action_id="act-100",
                source_action="源群消息 act-100",
                reason="手动屏蔽测试来源",
            ),
            "admin-a",
        )
        rule_version = session.get(RuleSetVersion, 42)
        other_task = session.get(Task, "relay-other-task")
        audit_log = session.scalar(select(AuditLog).where(AuditLog.target_id == "relay-source-override").order_by(AuditLog.id.desc()))

    assert updated.type_config["excluded_sender_peer_ids"] == ["old-peer", "new-peer"]
    assert updated.type_config["excluded_sender_usernames"] == ["old_user", "new_user"]
    assert updated.type_config["excluded_sender_names"] == ["新来源"]
    assert other_task.type_config.get("excluded_sender_peer_ids", []) == []
    assert rule_version.filters == {"keyword_whitelist": ["公告"]}
    assert rule_version.version_note == "已发布版本不应被任务覆盖改写"
    assert audit_log is not None
    assert audit_log.actor == "admin-a"
    assert audit_log.action == "添加任务来源过滤覆盖"
    assert "new-peer" in audit_log.detail
    assert "new_user" in audit_log.detail
    assert "act-100" in audit_log.detail
    assert "手动屏蔽测试来源" in audit_log.detail


def test_group_relay_uses_source_group_accounts_when_monitor_accounts_empty(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    seen_account_ids: list[list[int]] = []

    def fake_collect(session: Session, group: TgGroup, account_ids: list[int] | None = None, **_kwargs) -> int:
        seen_account_ids.append(list(account_ids or []))
        session.add(
            GroupContextMessage(
                tenant_id=1,
                group_id=group.id,
                listener_account_id=(account_ids or [0])[0],
                sender_peer_id="real-user",
                sender_name="真实用户",
                content="公告：源群新消息需要转发",
                remote_message_id="relay-source-auto-account",
                sent_at=datetime(2026, 5, 11, 10, 0, 0),
            )
        )
        session.flush()
        return 1

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add_all(
            [
                TgAccount(id=101, tenant_id=1, display_name="源群账号", phone_masked="101", status="在线", health_score=80),
                TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="源群", auth_status="已授权运营", listener_context_limit=20),
                TgGroup(id=9, tenant_id=1, tg_peer_id="-1009", title="目标群", auth_status="已授权运营", listener_context_limit=20),
                TgGroupAccount(id=701, tenant_id=1, group_id=7, account_id=101, can_send=True, is_listener=False),
                TgGroupAccount(id=901, tenant_id=1, group_id=9, account_id=101, can_send=True, is_listener=False),
                Task(
                    id="relay-auto-monitor-account",
                    tenant_id=1,
                    name="自动监听账号",
                    type="group_relay",
                    status="running",
                    account_config={"selection_mode": "all", "max_concurrent": 1, "cooldown_per_account_minutes": 0},
                    pacing_config={"mode": "fixed", "interval_seconds_min": 0, "interval_seconds_max": 0, "jitter_percent": 0},
                    type_config={
                        "source_groups": [{"group_id": 7, "is_active": True}],
                        "target_group_id": 9,
                        "monitor_account_ids": [],
                        "content_mode": "raw",
                        "dedup_window_minutes": 60,
                    },
                ),
            ]
        )
        session.commit()

        monkeypatch.setattr("app.services.task_center.executors.group_relay.collect_group_context", fake_collect)
        monkeypatch.setattr("app.services.task_center.executors.group_relay.should_collect_listener", lambda *_args, **_kwargs: True)

        assert build_group_relay_plan(session, session.get(Task, "relay-auto-monitor-account")) == 1
        action = session.scalar(select(Action).where(Action.task_id == "relay-auto-monitor-account"))

    assert seen_account_ids == [[101]]
    assert action is not None
    assert action.account_id == 101


def test_channel_subtask_status_prefers_capacity_and_progress():
    assert _channel_subtask_status({"target_count": 50, "completed_count": 38, "running_count": 4, "capacity_shortfall": 8}) == "容量不足"
    assert _channel_subtask_status({"target_count": 50, "completed_count": 50, "running_count": 0, "capacity_shortfall": 0}) == "已达标"
    assert _channel_subtask_status({"target_count": 50, "completed_count": 10, "failed_count": 2, "running_count": 0, "capacity_shortfall": 0}) == "有失败"


def test_channel_like_jitter_uses_available_accounts_without_false_capacity(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        accounts = [
            TgAccount(
                id=account_id,
                tenant_id=1,
                display_name=f"账号{account_id}",
                phone_masked=str(account_id),
                status=AccountStatus.ACTIVE.value,
                health_score=100 - (account_id - 100),
                session_ciphertext=f"session-{account_id}",
            )
            for account_id in range(101, 106)
        ]
        channel = OperationTarget(id=21, tenant_id=1, target_type="channel", tg_peer_id="-10021", title="容量频道", username="capacity_channel", can_send=True, auth_status="已授权运营")
        message = ChannelMessage(id=31, tenant_id=1, channel_target_id=21, message_id=6101, message_url="https://t.me/capacity_channel/6101", content_preview="容量测试")

        def make_task(task_id: str) -> Task:
            return Task(
                id=task_id,
                tenant_id=1,
                name="抖动容量",
                type="channel_like",
                status="running",
                account_config={"selection_mode": "manual", "account_ids": [item.id for item in accounts], "max_concurrent": 5, "cooldown_per_account_minutes": 0},
                pacing_config={"mode": "fixed", "interval_seconds_min": 0, "interval_seconds_max": 0, "jitter_percent": 0},
                type_config={
                    "target_channel_id": channel.id,
                    "message_scope": "specific",
                    "message_ids": [message.id],
                    "target_likes_per_message": 3,
                    "like_count_jitter": 0.3,
                    "allowed_reactions": ["👍"],
                    "max_likes_per_account_per_hour": 999,
                },
                stats={},
            )

        upper_task = make_task("channel-like-jitter-capacity-upper")
        lower_task = make_task("channel-like-jitter-capacity-lower")
        session.add_all([*accounts, channel, message, upper_task, lower_task])
        session.commit()

        monkeypatch.setattr("app.services.task_center.executors.common.random.randint", lambda _lower, upper: upper)

        assert build_channel_like_plan(session, upper_task) == 4
        upper_detail = get_task_detail(session, 1, upper_task.id)

        monkeypatch.setattr("app.services.task_center.executors.common.random.randint", lambda lower, _upper: lower)

        assert build_channel_like_plan(session, lower_task) == 2
        lower_detail = get_task_detail(session, 1, lower_task.id)

    upper_group = upper_detail["message_groups"][0]
    assert len(upper_detail["actions"]) == 4
    assert len({action.account_id for action in upper_detail["actions"]}) == 4
    assert upper_group["target_count"] == 4
    assert upper_group["capacity_shortfall"] == 0
    assert upper_group["subtask_status"] == "运行中"
    assert "capacity_warning" not in upper_detail["task"]["stats"]

    lower_group = lower_detail["message_groups"][0]
    assert len(lower_detail["actions"]) == 2
    assert lower_group["target_count"] == 2
    assert lower_group["capacity_shortfall"] == 0
    assert lower_group["subtask_status"] == "运行中"
    assert "capacity_warning" not in lower_detail["task"]["stats"]


def test_channel_like_create_defaults_to_dynamic_new_scope():
    payload = ChannelLikeTaskCreate(name="默认持续点赞", target_channel_id=1)

    assert payload.message_scope == "dynamic_new"


def test_channel_view_and_comment_create_default_to_dynamic_new_scope():
    view_payload = ChannelViewTaskCreate(name="默认持续浏览", target_channel_id=1)
    comment_payload = ChannelCommentTaskCreate(name="默认持续评论", target_channel_id=1)

    assert view_payload.message_scope == "dynamic_new"
    assert comment_payload.message_scope == "dynamic_new"


def test_channel_task_explicit_message_scope_is_preserved():
    view_payload = ChannelViewTaskCreate(name="指定最新浏览", target_channel_id=1, message_scope="latest_n")
    comment_payload = ChannelCommentTaskCreate(name="指定消息评论", target_channel_id=1, message_scope="specific", message_ids=[1001])

    assert view_payload.message_scope == "latest_n"
    assert comment_payload.message_scope == "specific"


def test_channel_comment_reply_mode_requires_and_plans_reply_targets(monkeypatch):
    try:
        ChannelCommentTaskCreate(name="缺少回复目标", target_channel_id=1, comment_mode="reply")
    except Exception as exc:  # pydantic validation error
        assert "reply_to_message_ids" in str(exc)
    else:
        raise AssertionError("reply mode should require reply_to_message_ids")

    payload = ChannelCommentTaskCreate(name="指定评论回复", target_channel_id=1, comment_mode="reply", reply_to_message_ids=[8101, 8102])
    assert payload.reply_to_message_ids == [8101, 8102]

    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    monkeypatch.setattr("app.services.task_center.executors.channel_comment.generate_channel_comments", lambda *_args, **_kwargs: (["回复 A", "回复 B"], 0))
    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgAccount(id=101, tenant_id=1, display_name="评论账号", phone_masked="101", status=AccountStatus.ACTIVE.value, health_score=100))
        session.add(OperationTarget(id=31, tenant_id=1, target_type="channel", tg_peer_id="-10031", title="频道目标", can_send=True, auth_status="已授权运营"))
        session.add(ChannelMessage(id=41, tenant_id=1, channel_target_id=31, message_id=9001, content_preview="频道消息"))
        session.add(ChannelMessageComment(tenant_id=1, channel_target_id=31, channel_message_id=41, comment_message_id=8101, author_name="用户 A"))
        session.add(ChannelMessageComment(tenant_id=1, channel_target_id=31, channel_message_id=41, comment_message_id=8102, author_name="用户 B"))
        task = Task(
            id="channel-reply-task",
            tenant_id=1,
            name="频道回复",
            type="channel_comment",
            status="running",
            account_config={"selection_mode": "manual", "account_ids": [101], "max_concurrent": 2, "cooldown_per_account_minutes": 0},
            pacing_config={"mode": "fixed", "interval_seconds_min": 0, "interval_seconds_max": 0, "jitter_percent": 0},
            type_config={
                "target_channel_id": 31,
                "message_scope": "specific",
                "message_ids": [41],
                "target_comments_per_message": 2,
                "comment_count_jitter": 0,
                "comment_mode": "reply",
                "reply_to_message_ids": [8101, 8102],
                "max_comments_per_account_per_hour": 500,
            },
            stats={},
        )
        session.add(task)
        session.commit()

        assert build_channel_comment_plan(session, task) == 2
        actions = sorted(session.scalars(select(Action).where(Action.task_id == task.id)), key=lambda item: item.payload["reply_to_message_id"])

    assert [action.payload["comment_mode"] for action in actions] == ["reply", "reply"]
    assert [action.payload["reply_to_message_id"] for action in actions] == [8101, 8102]
    assert actions[0].payload["reply_target_label"] == "回复消息 #8101"


def test_channel_comment_reply_targets_must_belong_to_selected_messages(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    monkeypatch.setattr("app.services.task_center.executors.channel_comment.generate_channel_comments", lambda *_args, **_kwargs: (["回复 A"], 0))
    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgAccount(id=101, tenant_id=1, display_name="评论账号", phone_masked="101", status=AccountStatus.ACTIVE.value, health_score=100))
        session.add(OperationTarget(id=31, tenant_id=1, target_type="channel", tg_peer_id="-10031", title="频道目标", can_send=True, auth_status="已授权运营"))
        session.add(ChannelMessage(id=41, tenant_id=1, channel_target_id=31, message_id=9001, content_preview="目标频道消息"))
        session.add(ChannelMessage(id=42, tenant_id=1, channel_target_id=31, message_id=9002, content_preview="其它频道消息"))
        session.add(ChannelMessageComment(tenant_id=1, channel_target_id=31, channel_message_id=42, comment_message_id=8201, author_name="其它消息评论"))
        task = Task(
            id="channel-reply-invalid-task",
            tenant_id=1,
            name="频道回复",
            type="channel_comment",
            status="running",
            account_config={"selection_mode": "manual", "account_ids": [101], "max_concurrent": 1, "cooldown_per_account_minutes": 0},
            pacing_config={"mode": "fixed", "interval_seconds_min": 0, "interval_seconds_max": 0, "jitter_percent": 0},
            type_config={
                "target_channel_id": 31,
                "message_scope": "specific",
                "message_ids": [41],
                "target_comments_per_message": 1,
                "comment_count_jitter": 0,
                "comment_mode": "reply",
                "reply_to_message_ids": [8201],
                "max_comments_per_account_per_hour": 500,
            },
            stats={},
        )
        session.add(task)
        session.commit()

        assert build_channel_comment_plan(session, task) == 0
        assert "回复对象不属于当前频道消息" in task.last_error
        assert session.scalars(select(Action).where(Action.task_id == task.id)).all() == []


def test_channel_comment_drops_ai_template_duplicates_and_missing_fillers(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    generated = [
        "这个内容挺有参考价值，先收藏一下。",
        "这个角度不错，值得再讨论一下。",
        "说得比较实在，后面可以继续展开看看。",
        "收纳盒这个尺寸有人实测过吗",
    ]
    monkeypatch.setattr("app.services.task_center.executors.channel_comment.generate_channel_comments", lambda *_args, **_kwargs: (generated, 0))
    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgAccount(id=101, tenant_id=1, display_name="评论账号", phone_masked="101", status=AccountStatus.ACTIVE.value, health_score=100))
        session.add(OperationTarget(id=31, tenant_id=1, target_type="channel", tg_peer_id="-10031", title="频道目标", can_send=True, auth_status="已授权运营"))
        session.add(ChannelMessage(id=41, tenant_id=1, channel_target_id=31, message_id=9001, content_preview="今天试了 18cm 收纳盒，塞进小柜子刚好"))
        task = Task(
            id="channel-comment-quality",
            tenant_id=1,
            name="频道评论质量",
            type="channel_comment",
            status="running",
            account_config={"selection_mode": "manual", "account_ids": [101], "max_concurrent": 4, "cooldown_per_account_minutes": 0},
            pacing_config={"mode": "fixed", "interval_seconds_min": 0, "interval_seconds_max": 0, "jitter_percent": 0},
            type_config={
                "target_channel_id": 31,
                "message_scope": "specific",
                "message_ids": [41],
                "target_comments_per_message": 4,
                "comment_count_jitter": 0,
                "max_comments_per_account_per_hour": 500,
            },
            stats={},
        )
        session.add(task)
        session.add(
            Action(
                id="old-comment",
                tenant_id=1,
                task_id=task.id,
                task_type="channel_comment",
                action_type="post_comment",
                status="success",
                account_id=101,
                payload={"comment_text": "这个内容挺有参考价值，先收藏一下。"},
            )
        )
        session.commit()

        created = build_channel_comment_plan(session, task)
        comments = [action.payload["comment_text"] for action in session.scalars(select(Action).where(Action.task_id == task.id, Action.status == "pending"))]

    assert created == 1
    assert comments == ["收纳盒这个尺寸有人实测过吗"]


def test_channel_comment_dedupes_same_message_beyond_recent_task_window(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    monkeypatch.setattr("app.services.task_center.executors.channel_comment.generate_channel_comments", lambda *_args, **_kwargs: (["收纳盒这个尺寸有人实测过吗"], 0))
    base_time = datetime(2026, 5, 24, 12, 0, 0)
    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgAccount(id=101, tenant_id=1, display_name="评论账号", phone_masked="101", status=AccountStatus.ACTIVE.value, health_score=100))
        session.add(OperationTarget(id=31, tenant_id=1, target_type="channel", tg_peer_id="-10031", title="频道目标", can_send=True, auth_status="已授权运营"))
        session.add(ChannelMessage(id=41, tenant_id=1, channel_target_id=31, message_id=9001, content_preview="今天试了 18cm 收纳盒，塞进小柜子刚好"))
        for index in range(25):
            session.add(ChannelMessage(id=100 + index, tenant_id=1, channel_target_id=31, message_id=9100 + index, content_preview=f"其它消息 {index}"))
        task = Task(
            id="channel-comment-history-window",
            tenant_id=1,
            name="频道评论历史窗口",
            type="channel_comment",
            status="running",
            account_config={"selection_mode": "manual", "account_ids": [101], "max_concurrent": 2, "cooldown_per_account_minutes": 0},
            pacing_config={"mode": "fixed", "interval_seconds_min": 0, "interval_seconds_max": 0, "jitter_percent": 0},
            type_config={
                "target_channel_id": 31,
                "message_scope": "specific",
                "message_ids": [41],
                "target_comments_per_message": 2,
                "comment_count_jitter": 0,
                "max_comments_per_account_per_hour": 500,
            },
            stats={},
        )
        session.add(task)
        session.add(
            Action(
                id="old-current-message-comment",
                tenant_id=1,
                task_id=task.id,
                task_type="channel_comment",
                action_type="post_comment",
                status="success",
                account_id=101,
                created_at=base_time,
                payload={"channel_message_id": 41, "comment_text": "收纳盒这个尺寸有人实测过吗"},
            )
        )
        for index in range(25):
            session.add(
                Action(
                    id=f"recent-other-message-comment-{index}",
                    tenant_id=1,
                    task_id=task.id,
                    task_type="channel_comment",
                    action_type="post_comment",
                    status="success",
                    account_id=101,
                    created_at=base_time + timedelta(minutes=index + 1),
                    payload={"channel_message_id": 100 + index, "comment_text": f"其它消息评论 {index}"},
                )
            )
        session.commit()

        assert build_channel_comment_plan(session, task) == 0
        pending = session.scalars(select(Action).where(Action.task_id == task.id, Action.status == "pending")).all()

    assert pending == []


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


def test_operation_profile_drives_schedule_and_ai_cycle_mode():
    pacing_config = {
        "operation_profile": {
            "hourly_activity_curve": [0, 0, 0, 0, 0, 0, 1, 2, 4, 6, 8, 10, 8, 6, 4, 3, 5, 7, 9, 10, 8, 5, 2, 1],
            "quiet_threshold": 2,
            "peak_threshold": 8,
        }
    }
    times = schedule_times(6, pacing_config, start_at=datetime(2026, 5, 11, 1, 10))
    assert len(times) == 6
    assert all(item.hour >= 6 for item in times)
    assert ai_cycle_mode({"pacing_config": pacing_config}, now=datetime(2026, 5, 11, 1, 30)) == ("休眠期", 0.0)
    assert ai_cycle_mode({"pacing_config": pacing_config}, now=datetime(2026, 5, 11, 6, 30)) == ("低频期", 0.05)
    assert ai_cycle_mode({"pacing_config": pacing_config}, now=datetime(2026, 5, 11, 11, 30)) == ("高峰期", 0.1)


def test_group_ai_chat_round_uses_near_term_schedule_with_operation_curve(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    round_start = datetime(2026, 5, 11, 22, 13, 0)

    def fake_generate_group_messages(_session, _tenant_id, _config, *, count, target_label, history):
        samples = ["线上吧", "微信视频可以", "QQ语音也行", "八点后方便", "风大别出门", "先拉个群", "我看行", "电脑开会稳", "手机也能听", "定个时间"]
        return samples[:count], 0

    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat._now", lambda: round_start)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.should_collect_listener", lambda *_args, **_kwargs: False)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.generate_group_messages", fake_generate_group_messages)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="活群", auth_status="已授权运营"))
        for account_id in range(101, 111):
            session.add(TgAccount(id=account_id, tenant_id=1, display_name=f"账号{account_id}", phone_masked=str(account_id), status="在线"))
            session.add(TgGroupAccount(tenant_id=1, group_id=7, account_id=account_id, can_send=True))
        session.add(
            GroupContextMessage(
                tenant_id=1,
                group_id=7,
                listener_account_id=101,
                sender_name="真人",
                content="线上还是线下",
                remote_message_id="real-context",
                sent_at=round_start - timedelta(minutes=1),
            )
        )
        task = Task(
            id="ai-near-round",
            tenant_id=1,
            name="活群近端排程",
            type="group_ai_chat",
            status="running",
            account_config={"selection_mode": "all", "max_concurrent": 50, "cooldown_per_account_minutes": 0},
            pacing_config={
                "mode": "template",
                "operation_profile": {
                    "hourly_activity_curve": [2, 2, 1, 1, 0, 0, 1, 2, 4, 5, 6, 6, 5, 4, 6, 7, 8, 9, 10, 10, 8, 6, 4, 3],
                    "quiet_threshold": 2,
                    "peak_threshold": 8,
                },
            },
            type_config={"target_group_id": 7, "messages_per_round_mode": "manual", "messages_per_round": 10, "participation_rate": 1, "participation_jitter": 0, "fact_anchor_required": False},
        )
        session.add(task)
        session.commit()

        assert build_group_ai_chat_plan(session, task) == 10
        actions = list(session.scalars(select(Action).where(Action.task_id == task.id).order_by(Action.scheduled_at.asc())))

    assert len(actions) == 10
    assert min(action.scheduled_at for action in actions) >= round_start
    assert max(action.scheduled_at for action in actions) <= round_start + timedelta(hours=1)


def test_group_ai_chat_bootstraps_without_history(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    captured: dict[str, object] = {}

    def fake_generate_group_messages(_session, _tenant_id, _config, *, count, target_label, history):
        captured["count"] = count
        captured["target_label"] = target_label
        captured["history"] = history
        captured["account_personas"] = _config.get("account_personas")
        return ["新人刚进群可以先打个招呼。", "今天群里有人想了解活动安排吗？", "我看大家可以先从常见问题聊起。"][:count], 0

    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.should_collect_listener", lambda *_args, **_kwargs: False)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.generate_group_messages", fake_generate_group_messages)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="新群", auth_status="已授权运营", topic_direction="新人欢迎和日常问候"))
        for account_id in [101, 102, 103, 104]:
            session.add(TgAccount(id=account_id, tenant_id=1, display_name=f"账号{account_id}", phone_masked=str(account_id), status="在线"))
            session.add(TgGroupAccount(tenant_id=1, group_id=7, account_id=account_id, can_send=True))
        session.add(
            Task(
                id="ai-bootstrap",
                tenant_id=1,
                name="AI 无上下文开场",
                type="group_ai_chat",
                status="running",
                account_config={"selection_mode": "all", "max_concurrent": 20, "cooldown_per_account_minutes": 0},
                pacing_config={"mode": "fixed", "interval_seconds_min": 0, "interval_seconds_max": 0, "jitter_percent": 0},
                type_config={"target_group_id": 7, "messages_per_round_mode": "auto", "topic_hint": "", "silent_mode_enabled": False, "account_personas": {"101": "欢迎新人账号", "102": "提问型账号"}},
            )
        )
        session.commit()

        created = build_group_ai_chat_plan(session, session.get(Task, "ai-bootstrap"))
        actions = list(session.scalars(select(Action).where(Action.task_id == "ai-bootstrap").order_by(Action.created_at.asc())))
        task = session.get(Task, "ai-bootstrap")
        stats = dict(task.stats or {})
        last_error = task.last_error

    assert created == 3
    assert captured["count"] == 3
    assert captured["account_personas"] == {"101": "欢迎新人账号", "102": "提问型账号"}
    assert "新人欢迎和日常问候" in str(captured["history"])
    assert stats["context_mode"] == "bootstrap"
    assert last_error == ""
    assert [action.account_id for action in actions] == [101, 102, 103]
    assert [action.payload["account_role"] for action in actions] == ["欢迎新人账号", "提问型账号", "提问型账号"]
    assert all(action.payload["review_approved"] is True for action in actions)
    assert all(action.payload["context_message_ids"] == [] for action in actions)
    assert all(action.payload["context_snapshot_message_id"] is None for action in actions)


def test_group_ai_chat_uses_recent_account_memory(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    captured: dict[str, object] = {}

    def fake_generate_group_messages(_session, _tenant_id, _config, *, count, target_label, history):
        captured["account_memories"] = _config.get("account_memories")
        captured["account_profiles"] = _config.get("account_profiles")
        captured["topic_thread"] = _config.get("topic_thread")
        captured["topic_plan"] = _config.get("topic_plan")
        return ["延续自己之前说的报名时间。", "我从另一个角度补一句。"][:count], 0

    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.should_collect_listener", lambda *_args, **_kwargs: False)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.generate_group_messages", fake_generate_group_messages)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgGroup(id=8, tenant_id=1, tg_peer_id="-1008", title="记忆测试群", auth_status="已授权运营"))
        for account_id in [101, 102]:
            session.add(TgAccount(id=account_id, tenant_id=1, display_name=f"账号{account_id}", phone_masked=str(account_id), status="在线"))
            session.add(TgGroupAccount(tenant_id=1, group_id=8, account_id=account_id, can_send=True))
        session.add(
            GroupContextMessage(
                tenant_id=1,
                group_id=8,
                listener_account_id=101,
                sender_name="真人用户",
                content="报名时间这块有人问到具体安排。",
                remote_message_id="memory-real-context",
                sent_at=datetime(2026, 5, 11, 8, 5, 0),
            )
        )
        session.add(
            Task(
                id="ai-memory-older",
                tenant_id=1,
                name="历史活跃任务",
                type="group_ai_chat",
                status="completed",
                account_config={"selection_mode": "manual", "account_ids": [101]},
                type_config={"target_group_id": 8},
            )
        )
        session.add(
            Action(
                tenant_id=1,
                task_id="ai-memory-older",
                task_type="group_ai_chat",
                action_type="send_message",
                account_id=101,
                status="success",
                executed_at=datetime(2026, 5, 10, 8, 0, 0),
                payload={
                    "cycle_id": "ai-memory-older:cycle:1",
                    "turn_index": 1,
                    "account_role": "长期答疑账号",
                    "intent": "承接话题",
                    "message_text": "之前在另一个任务里提醒过资料要提前准备。",
                },
            )
        )
        session.add(
            Task(
                id="ai-memory",
                tenant_id=1,
                name="AI 账号记忆",
                type="group_ai_chat",
                status="running",
                account_config={"selection_mode": "all", "max_concurrent": 20, "cooldown_per_account_minutes": 0},
                pacing_config={"mode": "fixed", "interval_seconds_min": 0, "interval_seconds_max": 0, "jitter_percent": 0},
                type_config={
                    "target_group_id": 8,
                    "messages_per_round_mode": "manual",
                    "messages_per_round": 1,
                    "participation_rate": 1,
                    "participation_jitter": 0,
                    "silent_mode_enabled": False,
                    "account_memory_depth": 2,
                },
            )
        )
        session.add(
            Action(
                tenant_id=1,
                task_id="ai-memory",
                task_type="group_ai_chat",
                action_type="send_message",
                account_id=101,
                status="success",
                executed_at=datetime(2026, 5, 11, 8, 0, 0),
                payload={
                    "cycle_id": "ai-memory:cycle:1",
                    "turn_index": 1,
                    "account_role": "答疑账号",
                    "intent": "补充信息",
                    "message_text": "我之前说过报名时间大概在周五下午。",
                },
            )
        )
        session.commit()

        created = build_group_ai_chat_plan(session, session.get(Task, "ai-memory"))
        new_actions = list(
            session.scalars(
                select(Action)
                .where(Action.task_id == "ai-memory", Action.status == "pending")
                .order_by(Action.created_at.asc())
            )
        )
        detail = get_task_detail(session, 1, "ai-memory")

    assert created == 2
    assert "101" in captured["account_memories"]
    assert "报名时间" in captured["account_memories"]["101"]
    assert "跨任务 历史活跃任务" in captured["account_memories"]["101"]
    assert "资料要提前准备" in captured["account_memories"]["101"]
    assert "101" in captured["account_profiles"]
    assert "历史成功发言 2 次" in captured["account_profiles"]["101"]
    assert "常用角色" in captured["account_profiles"]["101"]
    assert "报名时间这块有人问到具体安排" in captured["topic_thread"]
    assert "我之前说过报名时间大概在周五下午" in captured["topic_thread"]
    assert "承接" in captured["topic_plan"]
    assert "补充" in captured["topic_plan"]
    assert new_actions[0].payload["account_memory"]
    assert "报名时间" in new_actions[0].payload["account_memory"]
    assert "资料要提前准备" in new_actions[0].payload["account_memory"]
    assert "历史成功发言 2 次" in new_actions[0].payload["account_profile"]
    assert new_actions[0].payload["topic_thread"] == captured["topic_thread"]
    assert new_actions[0].payload["topic_plan"] == captured["topic_plan"]
    assert new_actions[1].payload["account_memory"] == ""
    assert detail["ai_generation_records"][0]["generation_id"] == new_actions[0].payload["ai_generation_id"]
    assert detail["ai_generation_records"][0]["generated_count"] == 2
    assert detail["ai_generation_records"][0]["account_memory_count"] == 1
    assert detail["ai_account_profiles"][0]["account_id"] == 101
    assert detail["ai_account_profiles"][0]["total_success_count"] == 2
    assert detail["ai_account_profiles"][0]["cross_task_success_count"] == 1
    generated_cycle = next(item for item in detail["ai_cycles"] if item["cycle_id"] == new_actions[0].payload["cycle_id"])
    assert generated_cycle["turns"][0]["account_memory"] == new_actions[0].payload["account_memory"]
    assert generated_cycle["turns"][0]["account_profile"] == new_actions[0].payload["account_profile"]
    assert generated_cycle["turns"][0]["topic_thread"] == new_actions[0].payload["topic_thread"]
    assert generated_cycle["turns"][0]["topic_plan"] == new_actions[0].payload["topic_plan"]


def test_group_ai_chat_generation_uses_healthy_provider_and_model_override(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    captured: dict[str, object] = {}

    def fake_generate_drafts(credentials, prompt, *, count, topic, tone, persona_set, temperature, max_tokens, **_kwargs):  # noqa: ANN001
        captured["provider_name"] = credentials.provider_name
        captured["model_name"] = credentials.model_name
        captured["prompt"] = prompt
        captured["system_prompt"] = _kwargs.get("system_prompt")
        captured["count"] = count
        captured["topic"] = topic
        captured["tone"] = tone
        captured["temperature"] = temperature
        captured["max_tokens"] = max_tokens
        captured["timeout"] = _kwargs.get("timeout")
        return AiGenerationResult(
            candidates=[AiDraftCandidate(persona="A", content="这个点接得上，先轻轻聊两句。", risk_level="低")],
            usage=AiUsage(total_tokens=88, billable=True),
        )

    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.should_collect_listener", lambda *_args, **_kwargs: False)
    monkeypatch.setattr("app.services.task_center.ai_generator.ai_gateway.generate_drafts", fake_generate_drafts)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="MiMo 活跃群", auth_status="已授权运营"))
        session.add(TgAccount(id=101, tenant_id=1, display_name="账号101", phone_masked="101", status="在线"))
        session.add(TgGroupAccount(tenant_id=1, group_id=7, account_id=101, can_send=True))
        session.add(
            AiProvider(
                id=1,
                provider_name="MiMo",
                provider_type="openai_compatible",
                base_url="https://api.xiaomimimo.com/v1",
                model_name="mimo-v2.5",
                api_key_ciphertext=encrypt_secret("test-key"),
                is_active=True,
                health_status="健康",
            )
        )
        session.add(TenantAiSetting(tenant_id=1, default_provider_id=1, ai_enabled=True, temperature=0.6, max_tokens=1024))
        session.add(
            PromptTemplate(
                id=91,
                tenant_id=1,
                template_type="AI黑话词表",
                name="成人行业黑话",
                content="老师=妓女\n开课=开始营业",
                is_active=True,
            )
        )
        session.add(
            Task(
                id="ai-provider-model",
                tenant_id=1,
                name="AI 模型覆盖",
                type="group_ai_chat",
                status="running",
                account_config={"selection_mode": "manual", "account_ids": [101], "max_concurrent": 1, "cooldown_per_account_minutes": 0},
                pacing_config={"mode": "fixed", "interval_seconds_min": 0, "interval_seconds_max": 0, "jitter_percent": 0},
                type_config={
                    "target_group_id": 7,
                    "topic_hint": "MiMo 续聊",
                    "ai_model": "MiMo-V2.5",
                    "slang_prompt_template_id": 91,
                    "messages_per_round_mode": "manual",
                    "messages_per_round": 1,
                    "silent_mode_enabled": False,
                },
            )
        )
        session.commit()

        created = build_group_ai_chat_plan(session, session.get(Task, "ai-provider-model"))
        action = session.scalar(select(Action).where(Action.task_id == "ai-provider-model"))
        task = session.get(Task, "ai-provider-model")

    assert created == 1
    assert captured["provider_name"] == "MiMo"
    assert captured["model_name"] == "mimo-v2.5"
    assert captured["topic"] == "MiMo 续聊"
    assert captured["temperature"] == 0.75
    assert captured["max_tokens"] == 1024
    assert captured["timeout"] == 120
    assert "MiMo 活跃群" in str(captured["prompt"])
    assert "现场接话消息" in str(captured["prompt"])
    assert "sequence_index" in str(captured["prompt"])
    assert "大家怎么看" in str(captured["prompt"])
    assert "多数短句不要句号" in str(captured["prompt"])
    assert "截图里的真人聊天规律" in str(captured["prompt"])
    assert "8-24 个字" in str(captured["prompt"])
    assert "不要每句都补完整逗号和句号" in str(captured["system_prompt"])
    assert "短、碎、具体" in str(captured["system_prompt"])
    assert "AI 黑话配置：成人行业黑话" in str(captured["system_prompt"])
    assert "老师=妓女" in str(captured["system_prompt"])
    assert "开课=开始营业" in str(captured["system_prompt"])
    assert "老师=妓女" not in str(captured["prompt"])
    assert action is not None
    assert action.payload["message_text"] == "这个点接得上 先轻轻聊两句"
    assert action.payload["ai_generation_tokens"] == 88
    assert task.last_error == ""


def test_group_ai_chat_punctuation_cleanup_preserves_times_and_urls():
    cleaned = _humanize_group_chat_punctuation("9:30，到 https://example.com/a?x=1,2，可以看下。")

    assert cleaned == "9:30 到 https://example.com/a?x=1,2 可以看下"
    assert _humanize_group_chat_punctuation("我觉得,这个可以。") == "我觉得 这个可以"


def test_group_ai_chat_invalid_slang_template_sets_visible_error(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.should_collect_listener", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        "app.services.task_center.ai_generator.ai_gateway.generate_drafts",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("invalid slang template must stop before AI call")),
    )

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="黑话失效群", auth_status="已授权运营"))
        session.add(TgAccount(id=101, tenant_id=1, display_name="账号101", phone_masked="101", status="在线"))
        session.add(TgGroupAccount(tenant_id=1, group_id=7, account_id=101, can_send=True))
        session.add(
            Task(
                id="ai-invalid-slang",
                tenant_id=1,
                name="黑话配置失效",
                type="group_ai_chat",
                status="running",
                account_config={"selection_mode": "manual", "account_ids": [101], "max_concurrent": 1, "cooldown_per_account_minutes": 0},
                type_config={
                    "target_group_id": 7,
                    "slang_prompt_template_id": 404,
                    "messages_per_round_mode": "manual",
                    "messages_per_round": 1,
                    "silent_mode_enabled": False,
                },
            )
        )
        session.commit()

        created = build_group_ai_chat_plan(session, session.get(Task, "ai-invalid-slang"))
        task = session.get(Task, "ai-invalid-slang")

    assert created == 0
    assert task.last_error == "AI 黑话配置不存在或已禁用"


def test_group_ai_chat_model_override_selects_matching_deepseek_provider(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    captured: dict[str, object] = {}

    def fake_generate_drafts(credentials, prompt, *, count, topic, tone, persona_set, temperature, max_tokens, **_kwargs):  # noqa: ANN001
        captured["provider_name"] = credentials.provider_name
        captured["base_url"] = credentials.base_url
        captured["model_name"] = credentials.model_name
        captured["prompt"] = prompt
        return AiGenerationResult(
            candidates=[AiDraftCandidate(persona="A", content="DeepSeek 这轮也能正常接话。", risk_level="低")],
            usage=AiUsage(total_tokens=66, billable=True),
        )

    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.should_collect_listener", lambda *_args, **_kwargs: False)
    monkeypatch.setattr("app.services.task_center.ai_generator.ai_gateway.generate_drafts", fake_generate_drafts)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="DeepSeek 活跃群", auth_status="已授权运营"))
        session.add(TgAccount(id=101, tenant_id=1, display_name="账号101", phone_masked="101", status="在线"))
        session.add(TgGroupAccount(tenant_id=1, group_id=7, account_id=101, can_send=True))
        session.add(
            AiProvider(
                id=1,
                provider_name="Xiaomi MiMo",
                provider_type="openai_compatible",
                base_url="https://token-plan-cn.xiaomimimo.com/v1",
                model_name="mimo-v2.5",
                api_key_ciphertext=encrypt_secret("mimo-key"),
                is_active=True,
                health_status="健康",
            )
        )
        session.add(
            AiProvider(
                id=2,
                provider_name="DeepSeek",
                provider_type="openai_compatible",
                base_url="https://api.deepseek.com",
                model_name="deepseek-v4-flash",
                api_key_ciphertext=encrypt_secret("deepseek-key"),
                is_active=True,
                health_status="健康",
            )
        )
        session.add(TenantAiSetting(tenant_id=1, default_provider_id=1, ai_enabled=True, temperature=0.6, max_tokens=1024))
        session.add(
            Task(
                id="ai-deepseek-provider-model",
                tenant_id=1,
                name="DeepSeek 模型覆盖",
                type="group_ai_chat",
                status="running",
                account_config={"selection_mode": "manual", "account_ids": [101], "max_concurrent": 1, "cooldown_per_account_minutes": 0},
                pacing_config={"mode": "fixed", "interval_seconds_min": 0, "interval_seconds_max": 0, "jitter_percent": 0},
                type_config={
                    "target_group_id": 7,
                    "topic_hint": "DeepSeek V4 续聊",
                    "ai_model": "DeepSeek V4 Flash",
                    "messages_per_round_mode": "manual",
                    "messages_per_round": 1,
                    "silent_mode_enabled": False,
                },
            )
        )
        session.commit()

        created = build_group_ai_chat_plan(session, session.get(Task, "ai-deepseek-provider-model"))
        action = session.scalar(select(Action).where(Action.task_id == "ai-deepseek-provider-model"))

    assert created == 1
    assert captured["provider_name"] == "DeepSeek"
    assert captured["base_url"] == "https://api.deepseek.com"
    assert captured["model_name"] == "deepseek-v4-flash"
    assert "DeepSeek 活跃群" in str(captured["prompt"])
    assert action is not None
    assert action.payload["message_text"] == "DeepSeek 这轮也能正常接话"
    assert action.payload["ai_generation_tokens"] == 66


def test_group_ai_chat_without_ai_provider_does_not_create_actions(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.should_collect_listener", lambda *_args, **_kwargs: False)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="新群", auth_status="已授权运营", topic_direction="新人欢迎和日常问候"))
        session.add(TgAccount(id=101, tenant_id=1, display_name="账号101", phone_masked="101", status="在线"))
        session.add(TgGroupAccount(tenant_id=1, group_id=7, account_id=101, can_send=True))
        session.add(
            Task(
                id="ai-no-provider",
                tenant_id=1,
                name="AI 不可用不发",
                type="group_ai_chat",
                status="running",
                account_config={"selection_mode": "all", "max_concurrent": 20, "cooldown_per_account_minutes": 0},
                pacing_config={"mode": "fixed", "interval_seconds_min": 0, "interval_seconds_max": 0, "jitter_percent": 0},
                type_config={"target_group_id": 7, "messages_per_round_mode": "auto", "topic_hint": ""},
            )
        )
        session.commit()

        created = build_group_ai_chat_plan(session, session.get(Task, "ai-no-provider"))
        task = session.get(Task, "ai-no-provider")
        action_count = session.scalar(select(Action.id).where(Action.task_id == "ai-no-provider").limit(1))

    assert created == 0
    assert action_count is None
    assert task.status == "running"
    assert task.last_error == "AI 生成不可用，等待恢复后继续执行：租户 AI 配置不存在"


def test_group_ai_chat_filters_recursive_context_and_duplicate_ai_drafts(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    captured_prompt: dict[str, str] = {}

    def fake_generate_drafts(_credentials, prompt, **_kwargs):
        captured_prompt["prompt"] = prompt
        return AiGenerationResult(
            candidates=[
                AiDraftCandidate(persona="模板号", content="刚看到大家提到“刚看到大家提到“安师大”，这个点挺有意思，可以继续聊聊。”，这个点挺有意思，可以继续聊聊。"),
                AiDraftCandidate(persona="营销模板号", content="顺着这个话题说，精品榜榜单，有经验的朋友也可以补充下。"),
                AiDraftCandidate(persona="按钮说明号", content="点击底部按钮可以打开更多功能，大家怎么看？"),
                AiDraftCandidate(persona="占位符号", content="X老师那边听说还可以，某某校区也不错。"),
                AiDraftCandidate(persona="重复号", content="安师大新校区我上次路过还挺热闹。"),
                AiDraftCandidate(persona="重复号2", content="安师大新校区我上次路过还挺热闹！"),
                AiDraftCandidate(persona="自然号", content="安师大新校区那边最近是不是活动挺多的？"),
            ],
            usage=AiUsage(total_tokens=12),
        )

    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.should_collect_listener", lambda *_args, **_kwargs: False)
    monkeypatch.setattr("app.services.task_center.ai_generator.ai_gateway.generate_drafts", fake_generate_drafts)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(AiProvider(id=1, provider_name="Xiaomi MiMo", provider_type="openai_compatible", base_url="mock://xiaomimimo", model_name="mimo-v2.5", api_key_ciphertext=encrypt_secret("mock"), health_status="健康"))
        session.add(TenantAiSetting(tenant_id=1, default_provider_id=1, ai_enabled=True, temperature=0.8, max_tokens=1024))
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="新群", auth_status="已授权运营", topic_direction="校园日常"))
        for account_id in [101, 102, 103]:
            session.add(TgAccount(id=account_id, tenant_id=1, display_name=f"账号{account_id}", phone_masked=str(account_id), status="在线"))
            session.add(TgGroupAccount(tenant_id=1, group_id=7, account_id=account_id, can_send=True))
        session.add_all(
            [
                GroupContextMessage(id=41, tenant_id=1, group_id=7, listener_account_id=101, sender_name="真人用户", content="刚看到大家提到“刚看到大家提到“安师大”，这个点挺有意思，可以继续聊聊。”，这个点挺有意思，可以继续聊聊。", remote_message_id="bad", sent_at=datetime(2026, 5, 11, 10, 0, 0)),
                GroupContextMessage(id=42, tenant_id=1, group_id=7, listener_account_id=101, sender_name="真人用户", content="顺着这个话题说，点击底部按钮可以打开更多功能，有经验的朋友也可以补充下。", remote_message_id="bad-template", sent_at=datetime(2026, 5, 11, 10, 1, 0)),
                GroupContextMessage(id=43, tenant_id=1, group_id=7, listener_account_id=101, sender_name="系统提示", content="还没有你的定位。为了保护隐私，请点下面按钮到私聊更新定位。更新后回到本群发送“附近”，就能查询附近老师。", remote_message_id="location-noise", sent_at=datetime(2026, 5, 11, 10, 2, 0)),
                GroupContextMessage(id=44, tenant_id=1, group_id=7, listener_account_id=101, sender_name="真人用户", content="安师大", remote_message_id="real", sent_at=datetime(2026, 5, 11, 10, 3, 0)),
            ]
        )
        session.add(
            Task(
                id="ai-natural",
                tenant_id=1,
                name="AI 自然续聊",
                type="group_ai_chat",
                status="running",
                account_config={"selection_mode": "all", "max_concurrent": 20, "cooldown_per_account_minutes": 0},
                pacing_config={"mode": "fixed", "interval_seconds_min": 0, "interval_seconds_max": 0, "jitter_percent": 0},
                type_config={"target_group_id": 7, "messages_per_round_mode": "manual", "messages_per_round": 2, "topic_hint": ""},
            )
        )
        session.commit()

        created = build_group_ai_chat_plan(session, session.get(Task, "ai-natural"))
        actions = list(session.scalars(select(Action).where(Action.task_id == "ai-natural").order_by(Action.created_at.asc())))

    assert "安师大" in captured_prompt["prompt"]
    assert "刚看到大家提到“刚看到大家提到" not in captured_prompt["prompt"]
    assert "真人用户: 顺着这个话题说" not in captured_prompt["prompt"]
    assert "点击底部按钮" not in captured_prompt["prompt"]
    assert "还没有你的定位" not in captured_prompt["prompt"]
    assert created == 2
    assert [action.payload["message_text"] for action in actions] == [
        "安师大新校区我上次路过还挺热闹",
        "安师大新校区那边最近是不是活动挺多的？",
    ]


def test_group_ai_chat_context_prefers_topic_relevant_messages():
    rows = [
        SimpleNamespace(content="郑州精品必吃榜，踩坑包赔！"),
        SimpleNamespace(content="老师"),
        SimpleNamespace(content="老师质量这块我更看课后反馈。"),
    ]

    filtered = _topic_relevant_context_rows({"topic_hint": "老师质量"}, rows)

    assert [row.content for row in filtered] == ["老师", "老师质量这块我更看课后反馈。"]


def test_group_ai_chat_waits_when_no_new_real_context(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    generated: list[str] = []

    def fake_generate_group_messages(_session, _tenant_id, _config, *, count, target_label, history):
        generated.append(history)
        return ["这条真人消息可以接着问细节。"], 0

    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.should_collect_listener", lambda *_args, **_kwargs: False)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.generate_group_messages", fake_generate_group_messages)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="新群", auth_status="已授权运营", topic_direction="校园日常"))
        session.add(TgAccount(id=101, tenant_id=1, display_name="账号101", phone_masked="101", status="在线"))
        session.add(TgGroupAccount(tenant_id=1, group_id=7, account_id=101, can_send=True))
        session.add(
            GroupContextMessage(
                id=43,
                tenant_id=1,
                group_id=7,
                listener_account_id=101,
                sender_name="真人用户",
                content="这条真人消息",
                remote_message_id="real-once",
                sent_at=datetime(2026, 5, 11, 10, 2, 0),
            )
        )
        session.add(
            Task(
                id="ai-wait-new",
                tenant_id=1,
                name="AI 等待新上下文",
                type="group_ai_chat",
                status="running",
                account_config={"selection_mode": "all", "max_concurrent": 20, "cooldown_per_account_minutes": 0},
                pacing_config={"mode": "fixed", "interval_seconds_min": 0, "interval_seconds_max": 0, "jitter_percent": 0},
                type_config={"target_group_id": 7, "messages_per_round_mode": "auto", "topic_hint": ""},
            )
        )
        session.commit()

        assert build_group_ai_chat_plan(session, session.get(Task, "ai-wait-new")) == 1
        assert build_group_ai_chat_plan(session, session.get(Task, "ai-wait-new")) == 0
        action_count = session.scalar(select(func.count(Action.id)).where(Action.task_id == "ai-wait-new"))
        task = session.get(Task, "ai-wait-new")

    assert len(generated) == 1
    assert action_count == 1
    assert task.last_error == "暂无新的真人上下文，等待群内新消息"
    assert task.stats["context_mode"] == "waiting_new_context"


def test_group_ai_chat_idle_continuation_waits_until_interval(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    generated: list[str] = []
    now_value = datetime(2026, 5, 13, 11, 0, 0)

    def fake_generate_group_messages(_session, _tenant_id, _config, *, count, target_label, history):
        generated.append(history)
        return [f"续聊内容 {len(generated)}"], 0

    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat._now", lambda: now_value)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.should_collect_listener", lambda *_args, **_kwargs: False)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.generate_group_messages", fake_generate_group_messages)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="新群", auth_status="已授权运营", topic_direction="校园日常"))
        session.add(TgAccount(id=101, tenant_id=1, display_name="账号101", phone_masked="101", status="在线"))
        session.add(TgGroupAccount(tenant_id=1, group_id=7, account_id=101, can_send=True))
        session.add(
            GroupContextMessage(
                id=43,
                tenant_id=1,
                group_id=7,
                listener_account_id=101,
                sender_name="真人用户",
                content="第一条真人消息",
                remote_message_id="real-once",
                sent_at=now_value - timedelta(minutes=10),
            )
        )
        session.add(
            Task(
                id="ai-idle-wait",
                tenant_id=1,
                name="AI 未到续聊间隔",
                type="group_ai_chat",
                status="running",
                account_config={"selection_mode": "all", "max_concurrent": 20, "cooldown_per_account_minutes": 0},
                pacing_config={"mode": "fixed", "interval_seconds_min": 0, "interval_seconds_max": 0, "jitter_percent": 0},
                type_config={"target_group_id": 7, "messages_per_round_mode": "manual", "messages_per_round": 1, "idle_continuation_seconds": 300},
            )
        )
        session.commit()

        assert build_group_ai_chat_plan(session, session.get(Task, "ai-idle-wait")) == 1
        action = session.scalar(select(Action).where(Action.task_id == "ai-idle-wait"))
        action.status = "success"
        action.executed_at = now_value
        action.result = {"success": True}
        session.commit()

        assert build_group_ai_chat_plan(session, session.get(Task, "ai-idle-wait")) == 0
        task = session.get(Task, "ai-idle-wait")
        action_count = session.scalar(select(func.count(Action.id)).where(Action.task_id == "ai-idle-wait"))

    assert len(generated) == 1
    assert action_count == 1
    assert task.status == "running"
    assert task.last_error == "持续监听中，等待新消息或空闲续聊间隔"
    assert task.stats["context_mode"] == "waiting_new_context"
    assert task.stats["idle_continuation_next_run_at"] == (now_value + timedelta(seconds=300)).isoformat()
    assert task.next_run_at == now_value + timedelta(seconds=300)


def test_group_ai_chat_idle_continuation_generates_after_interval(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    generated: list[str] = []
    now_value = datetime(2026, 5, 13, 11, 0, 0)

    def fake_generate_group_messages(_session, _tenant_id, _config, *, count, target_label, history):
        generated.append(history)
        if len(generated) == 1:
            return ["第一轮先接住真人消息。"], 0
        return ["这会儿人少，可以先问问有没有新情况。"], 0

    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat._now", lambda: now_value)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.should_collect_listener", lambda *_args, **_kwargs: False)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.generate_group_messages", fake_generate_group_messages)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="新群", auth_status="已授权运营", topic_direction="校园日常"))
        session.add(TgAccount(id=101, tenant_id=1, display_name="账号101", phone_masked="101", status="在线"))
        session.add(TgGroupAccount(tenant_id=1, group_id=7, account_id=101, can_send=True))
        session.add(
            GroupContextMessage(
                id=43,
                tenant_id=1,
                group_id=7,
                listener_account_id=101,
                sender_name="真人用户",
                content="第一条真人消息",
                remote_message_id="real-once",
                sent_at=now_value - timedelta(minutes=10),
            )
        )
        session.add(
            Task(
                id="ai-idle-due",
                tenant_id=1,
                name="AI 到点续聊",
                type="group_ai_chat",
                status="running",
                account_config={"selection_mode": "all", "max_concurrent": 20, "cooldown_per_account_minutes": 0},
                pacing_config={"mode": "fixed", "interval_seconds_min": 0, "interval_seconds_max": 0, "jitter_percent": 0},
                type_config={"target_group_id": 7, "messages_per_round_mode": "manual", "messages_per_round": 1, "idle_continuation_seconds": 300},
            )
        )
        session.commit()

        assert build_group_ai_chat_plan(session, session.get(Task, "ai-idle-due")) == 1
        first_action = session.scalar(select(Action).where(Action.task_id == "ai-idle-due"))
        first_action.status = "success"
        first_action.executed_at = now_value - timedelta(seconds=301)
        first_action.payload["message_text"] = "上一轮已经聊过开场。"
        first_action.result = {"success": True}
        session.commit()

        assert build_group_ai_chat_plan(session, session.get(Task, "ai-idle-due")) == 1
        task = session.get(Task, "ai-idle-due")
        actions = list(session.scalars(select(Action).where(Action.task_id == "ai-idle-due").order_by(Action.created_at.asc(), Action.id.asc())))

    assert len(generated) == 2
    assert "群内暂时没有新的真人消息" in generated[-1]
    assert "上一轮 AI 已说" in generated[-1]
    assert "不要编具体经历" in generated[-1]
    assert len(actions) == 2
    assert actions[-1].payload["message_text"] == "这会儿人少，可以先问问有没有新情况。"
    assert actions[-1].payload["chat_mode"] == "idle_warmup"
    assert actions[-1].payload["hallucination_risk"] == ""
    assert task.status == "running"
    assert task.last_error == ""
    assert task.stats["context_mode"] == "idle_continuation"
    assert "idle_continuation_next_run_at" not in task.stats


def test_group_ai_chat_rotates_single_turn_accounts_between_cycles(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = datetime(2026, 5, 13, 11, 0, 0)
    outputs = iter(["郑州这会儿还挺热吗", "是不是下午会凉一点"])

    def fake_generate_group_messages(_session, _tenant_id, _config, *, count, target_label, history):
        return [next(outputs)], 0

    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat._now", lambda: now_value)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.should_collect_listener", lambda *_args, **_kwargs: False)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.generate_group_messages", fake_generate_group_messages)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="新群", auth_status="已授权运营"))
        for account_id in [101, 102, 103]:
            session.add(TgAccount(id=account_id, tenant_id=1, display_name=f"账号{account_id}", phone_masked=str(account_id), status="在线"))
            session.add(TgGroupAccount(tenant_id=1, group_id=7, account_id=account_id, can_send=True))
        session.add(GroupContextMessage(id=43, tenant_id=1, group_id=7, listener_account_id=101, sender_name="真人用户", content="今天郑州天气咋样", remote_message_id="real-once", sent_at=now_value - timedelta(minutes=10)))
        session.add(
            Task(
                id="ai-rotate-single-turn",
                tenant_id=1,
                name="AI 单条轮换",
                type="group_ai_chat",
                status="running",
                account_config={"selection_mode": "all", "max_concurrent": 20, "cooldown_per_account_minutes": 0},
                pacing_config={"mode": "fixed", "interval_seconds_min": 0, "interval_seconds_max": 0, "jitter_percent": 0},
                type_config={"target_group_id": 7, "messages_per_round_mode": "manual", "participation_rate": 0.05, "messages_per_round": 1, "idle_continuation_seconds": 300, "fact_anchor_required": False},
            )
        )
        session.commit()

        assert build_group_ai_chat_plan(session, session.get(Task, "ai-rotate-single-turn")) == 1
        first_action = session.scalar(select(Action).where(Action.task_id == "ai-rotate-single-turn"))
        first_action.status = "success"
        first_action.executed_at = now_value - timedelta(seconds=301)
        first_action.result = {"success": True}
        session.commit()
        assert build_group_ai_chat_plan(session, session.get(Task, "ai-rotate-single-turn")) == 1
        actions = list(session.scalars(select(Action).where(Action.task_id == "ai-rotate-single-turn").order_by(Action.created_at.asc())))

    assert [action.account_id for action in actions] == [101, 102]


def test_group_ai_chat_turn_account_choice_prefers_unused_accounts():
    selected = [SimpleNamespace(id=101), SimpleNamespace(id=102), SimpleNamespace(id=103)]
    used = {101}

    first = _choose_turn_account([selected[0], selected[1]], selected, 1, used, True)
    used.add(first.id)
    second = _choose_turn_account([selected[1], selected[2]], selected, 2, used, True)

    assert first.id == 102
    assert second.id == 103


def test_group_ai_chat_manual_round_spreads_messages_across_accounts(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = datetime(2026, 5, 27, 11, 0, 0)

    def fake_generate_group_messages(_session, _tenant_id, _config, *, count, target_label, history):
        messages = [
            "郑州今天有点热啊",
            "下午会不会凉快点",
            "我刚看天气预报说有风",
            "晚上出去应该舒服些",
            "大家今天都忙啥呢",
            "刚下课的人多不多",
            "附近吃饭有推荐吗",
            "我想找个安静点的地方",
            "周末有人约活动吗",
            "先看看群里有没有人回应",
        ]
        return messages[:count], 0

    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat._now", lambda: now_value)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.should_collect_listener", lambda *_args, **_kwargs: False)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.generate_group_messages", fake_generate_group_messages)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="新群", auth_status="已授权运营"))
        for account_id in range(101, 121):
            session.add(TgAccount(id=account_id, tenant_id=1, display_name=f"账号{account_id}", phone_masked=str(account_id), status="在线"))
            session.add(TgGroupAccount(tenant_id=1, group_id=7, account_id=account_id, can_send=True))
        session.add(GroupContextMessage(id=43, tenant_id=1, group_id=7, listener_account_id=101, sender_name="真人用户", content="今天郑州天气咋样", remote_message_id="real-once", sent_at=now_value - timedelta(minutes=10)))
        session.add(
            Task(
                id="ai-spread-manual-round",
                tenant_id=1,
                name="AI 多账号发言",
                type="group_ai_chat",
                status="running",
                account_config={"selection_mode": "all", "max_concurrent": 20, "cooldown_per_account_minutes": 0},
                pacing_config={"mode": "fixed", "interval_seconds_min": 0, "interval_seconds_max": 0, "jitter_percent": 0},
                type_config={"target_group_id": 7, "messages_per_round_mode": "manual", "participation_rate": 0.05, "participation_jitter": 0, "messages_per_round": 10, "idle_continuation_seconds": 300, "fact_anchor_required": False},
            )
        )
        session.commit()

        assert build_group_ai_chat_plan(session, session.get(Task, "ai-spread-manual-round")) == 10
        actions = list(session.scalars(select(Action).where(Action.task_id == "ai-spread-manual-round").order_by(Action.created_at.asc())))

    assert len({action.account_id for action in actions}) == 10
    assert [action.account_id for action in actions] == list(range(101, 111))


def test_group_ai_chat_blocks_unanchored_idle_experience_claims(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    generated: list[str] = []
    now_value = datetime(2026, 5, 13, 11, 0, 0)

    def fake_generate_group_messages(_session, _tenant_id, _config, *, count, target_label, history):
        generated.append(history)
        if len(generated) == 1:
            return ["第一轮先接住真人消息。"], 0
        return ["走之前还确认了下 挺细心"], 0

    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat._now", lambda: now_value)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.should_collect_listener", lambda *_args, **_kwargs: False)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.generate_group_messages", fake_generate_group_messages)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="新群", auth_status="已授权运营", topic_direction="校园日常"))
        session.add(TgAccount(id=101, tenant_id=1, display_name="账号101", phone_masked="101", status="在线"))
        session.add(TgGroupAccount(tenant_id=1, group_id=7, account_id=101, can_send=True))
        session.add(
            GroupContextMessage(
                id=43,
                tenant_id=1,
                group_id=7,
                listener_account_id=101,
                sender_name="真人用户",
                content="第一条真人消息",
                remote_message_id="real-once",
                sent_at=now_value - timedelta(minutes=10),
            )
        )
        session.add(
            Task(
                id="ai-idle-hallucination",
                tenant_id=1,
                name="AI 空闲幻觉拦截",
                type="group_ai_chat",
                status="running",
                account_config={"selection_mode": "all", "max_concurrent": 20, "cooldown_per_account_minutes": 0},
                pacing_config={"mode": "fixed", "interval_seconds_min": 0, "interval_seconds_max": 0, "jitter_percent": 0},
                type_config={"target_group_id": 7, "messages_per_round_mode": "manual", "messages_per_round": 1, "idle_continuation_seconds": 300},
            )
        )
        session.commit()

        assert build_group_ai_chat_plan(session, session.get(Task, "ai-idle-hallucination")) == 1
        first_action = session.scalar(select(Action).where(Action.task_id == "ai-idle-hallucination"))
        first_action.status = "success"
        first_action.executed_at = now_value - timedelta(seconds=301)
        first_action.payload["message_text"] = "上一轮已经聊过开场。"
        first_action.result = {"success": True}
        session.commit()

        assert build_group_ai_chat_plan(session, session.get(Task, "ai-idle-hallucination")) == 0
        task = session.get(Task, "ai-idle-hallucination")
        action_count = session.scalar(select(func.count(Action.id)).where(Action.task_id == "ai-idle-hallucination"))

    assert len(generated) == 2
    assert action_count == 1
    assert task.stats["context_mode"] == "idle_continuation"
    assert task.stats["chat_mode"] == "idle_warmup"
    assert task.stats["skip_reason"] == "hallucination_risk"
    assert task.stats["hallucination_risk"] == "high"
    assert task.last_error == "AI 候选缺少事实锚点，已跳过本轮"


def test_group_ai_chat_semantic_clusters_drop_repeated_experience_templates(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    def fake_generate_group_messages(_session, _tenant_id, _config, *, count, target_label, history):
        return [
            "照片准是重点 上次真人没差",
            "照片没p 本人也差不多",
            "态度稳点真省心",
            "这个价格还是得自己问清楚",
        ][:count], 0

    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.should_collect_listener", lambda *_args, **_kwargs: False)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.generate_group_messages", fake_generate_group_messages)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="新群", auth_status="已授权运营", topic_direction="群内接话"))
        for account_id in [101, 102, 103, 104]:
            session.add(TgAccount(id=account_id, tenant_id=1, display_name=f"账号{account_id}", phone_masked=str(account_id), status="在线"))
            session.add(TgGroupAccount(tenant_id=1, group_id=7, account_id=account_id, can_send=True))
        session.add(
            GroupContextMessage(
                id=43,
                tenant_id=1,
                group_id=7,
                listener_account_id=101,
                sender_name="真人用户",
                content="芳名叫啥，价格自己问吗？",
                remote_message_id="real-price",
                sent_at=datetime(2026, 5, 13, 10, 0, 0),
            )
        )
        session.add(
            Task(
                id="ai-semantic-dedup",
                tenant_id=1,
                name="AI 语义去重",
                type="group_ai_chat",
                status="running",
                account_config={"selection_mode": "all", "max_concurrent": 20, "cooldown_per_account_minutes": 0},
                pacing_config={"mode": "fixed", "interval_seconds_min": 0, "interval_seconds_max": 0, "jitter_percent": 0},
                type_config={"target_group_id": 7, "messages_per_round_mode": "manual", "messages_per_round": 1, "participation_rate": 1, "participation_jitter": 0},
            )
        )
        session.commit()

        created = build_group_ai_chat_plan(session, session.get(Task, "ai-semantic-dedup"))
        actions = list(session.scalars(select(Action).where(Action.task_id == "ai-semantic-dedup").order_by(Action.created_at.asc())))
        task = session.get(Task, "ai-semantic-dedup")

    assert created == 3
    assert [action.payload["message_text"] for action in actions] == [
        "照片准是重点 上次真人没差",
        "态度稳点真省心",
        "这个价格还是得自己问清楚",
    ]
    assert [action.payload["semantic_cluster"] for action in actions] == [
        "photo_real_match",
        "stable_attitude",
        "",
    ]
    assert task.stats["duplicate_risk"] == "semantic_cluster"


def test_group_ai_chat_idle_continuation_can_be_disabled(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    generated: list[str] = []
    now_value = datetime(2026, 5, 13, 11, 0, 0)

    def fake_generate_group_messages(_session, _tenant_id, _config, *, count, target_label, history):
        generated.append(history)
        return ["只应该生成第一轮。"], 0

    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat._now", lambda: now_value)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.should_collect_listener", lambda *_args, **_kwargs: False)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.generate_group_messages", fake_generate_group_messages)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="新群", auth_status="已授权运营", topic_direction="校园日常"))
        session.add(TgAccount(id=101, tenant_id=1, display_name="账号101", phone_masked="101", status="在线"))
        session.add(TgGroupAccount(tenant_id=1, group_id=7, account_id=101, can_send=True))
        session.add(
            GroupContextMessage(
                id=43,
                tenant_id=1,
                group_id=7,
                listener_account_id=101,
                sender_name="真人用户",
                content="第一条真人消息",
                remote_message_id="real-once",
                sent_at=now_value - timedelta(minutes=10),
            )
        )
        session.add(
            Task(
                id="ai-idle-disabled",
                tenant_id=1,
                name="AI 关闭续聊",
                type="group_ai_chat",
                status="running",
                account_config={"selection_mode": "all", "max_concurrent": 20, "cooldown_per_account_minutes": 0},
                pacing_config={"mode": "fixed", "interval_seconds_min": 0, "interval_seconds_max": 0, "jitter_percent": 0},
                type_config={"target_group_id": 7, "messages_per_round_mode": "manual", "messages_per_round": 1, "idle_continuation_enabled": False, "idle_continuation_seconds": 300},
            )
        )
        session.commit()

        assert build_group_ai_chat_plan(session, session.get(Task, "ai-idle-disabled")) == 1
        first_action = session.scalar(select(Action).where(Action.task_id == "ai-idle-disabled"))
        first_action.status = "success"
        first_action.executed_at = now_value - timedelta(hours=1)
        first_action.result = {"success": True}
        session.commit()

        assert build_group_ai_chat_plan(session, session.get(Task, "ai-idle-disabled")) == 0
        task = session.get(Task, "ai-idle-disabled")
        action_count = session.scalar(select(func.count(Action.id)).where(Action.task_id == "ai-idle-disabled"))

    assert len(generated) == 1
    assert action_count == 1
    assert task.status == "running"
    assert task.last_error == "暂无新的真人上下文，等待群内新消息"
    assert "idle_continuation_next_run_at" not in task.stats


def test_task_center_drain_respects_ai_idle_continuation_next_run(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    SessionFactory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    now_value = _now()
    idle_next = now_value + timedelta(minutes=5)

    def fake_build_task_plan(_session, task):
        stats = dict(task.stats or {})
        stats["context_mode"] = "waiting_new_context"
        stats["idle_continuation_next_run_at"] = idle_next.isoformat()
        task.stats = stats
        task.last_error = "持续监听中，等待新消息或空闲续聊间隔"
        return 0

    monkeypatch.setattr("app.services.task_center.service.build_task_plan", fake_build_task_plan)
    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(
            Task(
                id="ai-idle-drain",
                tenant_id=1,
                name="AI drain 续聊间隔",
                type="group_ai_chat",
                status="running",
                next_run_at=now_value - timedelta(seconds=1),
                pacing_config={"mode": "fixed", "interval_seconds_min": 0, "interval_seconds_max": 0, "jitter_percent": 0},
                type_config={"target_group_id": 7},
                stats={},
            )
        )
        session.commit()

    drain_task_center(SessionFactory, 10)

    with Session(engine) as session:
        task = session.get(Task, "ai-idle-drain")

    assert task.status == "running"
    assert task.last_error == "持续监听中，等待新消息或空闲续聊间隔"
    assert task.next_run_at == idle_next


def test_task_center_scheduled_end_marks_running_task_completed():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    SessionFactory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    now = datetime.now(UTC).replace(tzinfo=None)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        task = Task(
            tenant_id=1,
            name="到点自然结束",
            type="channel_like",
            status="running",
            scheduled_end=now - timedelta(seconds=1),
            next_run_at=now - timedelta(seconds=1),
            account_config={},
            pacing_config={},
            failure_policy={},
            type_config={},
            stats={},
        )
        session.add(task)
        session.commit()
        task_id = task.id

    drain_task_center(SessionFactory, 10)

    with Session(engine) as session:
        assert session.get(Task, task_id).status == "completed"


def test_task_center_recovers_stale_ai_task_waiting_for_context(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    SessionFactory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    planned: list[str] = []

    def fake_build_task_plan(_session, task):
        planned.append(task.id)
        return 0

    monkeypatch.setattr("app.services.task_center.service.build_task_plan", fake_build_task_plan)
    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(
            Task(
                id="ai-stale-context",
                tenant_id=1,
                name="AI 卡住任务",
                type="group_ai_chat",
                status="running",
                next_run_at=None,
                last_error="暂无群上下文，等待监听采集",
                type_config={"target_group_id": 7},
                stats={},
            )
        )
        session.commit()

    assert drain_task_center(SessionFactory, 10) >= 1
    with Session(engine) as session:
        task = session.get(Task, "ai-stale-context")
        assert task.status == "running"
        assert task.last_error == ""
        assert task.next_run_at is not None
    assert planned == ["ai-stale-context"]


def test_task_center_recovers_completed_channel_dynamic_tasks_without_end_time(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    SessionFactory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

    monkeypatch.setattr("app.services.task_center.service.build_task_plan", lambda *_args, **_kwargs: 0)
    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add_all(
            [
                *[
                    Task(
                        id=f"{task_type}-continuous",
                        tenant_id=1,
                        name=f"无结束时间{task_type}",
                        type=task_type,
                        status="completed",
                        scheduled_end=None,
                        next_run_at=None,
                        last_error="旧逻辑误完成",
                        type_config={"message_scope": "dynamic_new"},
                        stats={},
                    )
                    for task_type in ("channel_view", "channel_like", "channel_comment")
                ],
                Task(
                    id="channel-like-specific",
                    tenant_id=1,
                    name="指定消息点赞",
                    type="channel_like",
                    status="completed",
                    scheduled_end=None,
                    next_run_at=None,
                    type_config={"message_scope": "specific", "message_ids": [1]},
                    stats={},
                ),
            ]
        )
        session.commit()

    assert drain_task_center(SessionFactory, 10) >= 1
    with Session(engine) as session:
        recovered_tasks = [
            session.get(Task, f"{task_type}-continuous")
            for task_type in ("channel_view", "channel_like", "channel_comment")
        ]
        specific = session.get(Task, "channel-like-specific")
        for recovered in recovered_tasks:
            assert recovered.status == "running"
            assert recovered.last_error == ""
            assert recovered.next_run_at is not None
        assert specific.status == "completed"


def test_operation_metrics_summary_uses_real_task_center_tables():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add_all(
            [
                TgAccount(tenant_id=1, display_name="在线号", phone_masked="+861***0001", status="在线", health_score=98),
                TgAccount(tenant_id=1, display_name="异常号", phone_masked="+861***0002", status="异常", health_score=40),
                TgGroup(
                    id=1,
                    tenant_id=1,
                    tg_peer_id="g1",
                    title="目标群",
                    can_send=True,
                    daily_limit=88,
                    account_cooldown_seconds=120,
                    group_cooldown_seconds=60,
                    banned_words="敏感词",
                    link_whitelist="telema.cn",
                ),
                OperationTarget(tenant_id=1, target_type="group", tg_peer_id="g1", title="目标群", can_send=True, auth_status="已授权运营"),
                OperationTarget(tenant_id=1, target_type="channel", tg_peer_id="c1", title="频道", can_send=False, auth_status="已授权运营"),
                SchedulingSetting(
                    tenant_id=1,
                    quiet_hours_enabled=True,
                    quiet_start="01:00",
                    quiet_end="07:00",
                    default_on_content_rejected="rewrite_and_retry",
                ),
                ContentKeywordRule(id=11, tenant_id=1, keyword="敏感词", match_type="contains", is_active=True),
                Task(
                    id="task-ai",
                    tenant_id=1,
                    name="AI 活跃",
                    type="group_ai_chat",
                    status="running",
                    pacing_config={"max_actions_per_hour": 12, "quiet_hours": {"start": "01:00", "end": "07:00"}},
                    account_config={"cooldown_per_account_minutes": 3},
                ),
                Task(id="task-relay", tenant_id=1, name="转发监听", type="group_relay", status="running"),
                Action(id="a1", tenant_id=1, task_id="task-ai", task_type="group_ai_chat", action_type="send_message", status="success", executed_at=datetime(2026, 5, 11, 1, 0, 0)),
                Action(
                    id="a2",
                    tenant_id=1,
                    task_id="task-relay",
                    task_type="group_relay",
                    action_type="send_message",
                    account_id=2,
                    status="failed",
                    payload={
                        "target_display": "目标群",
                        "relay_event_id": "event:7:abc",
                        "source_group_id": 7,
                        "rule_set_id": 31,
                        "rule_set_version_id": 32,
                    },
                    result={"error_message": "账号受限"},
                ),
                Action(id="a4", tenant_id=1, task_id="task-ai", task_type="group_ai_chat", action_type="send_message", status="skipped", result={"error_message": "命中租户关键词：敏感词"}),
                Action(id="a5", tenant_id=1, task_id="task-ai", task_type="group_ai_chat", action_type="send_message", status="failed", result={"error_message": "群冷却中，还需等待 60 秒"}),
                Action(id="a3", tenant_id=1, task_id="task-relay", task_type="group_relay", action_type="like_message", status="success"),
                MessageFingerprint(tenant_id=1, source_group_id="1", fingerprint="abc", original_text="重复内容"),
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
    assert summary.account_details[0].title == "异常号"
    assert summary.target_details[0].title == "频道"
    assert summary.task_details[0].related_id in {"task-ai", "task-relay"}
    relay_failure = next(item for item in summary.failure_details if item.related_id == "a2")
    assert "事件 event:7:abc" in relay_failure.detail
    assert "规则集 #31 / 版本 #32" in relay_failure.detail
    assert "账号 #2" in relay_failure.detail
    assert next(item.value for item in summary.risk_control if item.key == "risk.quiet_hours") == "01:00-07:00"
    assert next(item.value for item in summary.risk_control if item.key == "risk.keyword_rules") == 1
    assert next(item.value for item in summary.risk_control if item.key == "risk.task_rate_limits") == 1
    assert next(item.value for item in summary.risk_control if item.key == "risk.content_rejected") == 1
    assert next(item.value for item in summary.risk_control if item.key == "risk.rate_limited") == 1
    assert next(item.value for item in summary.risk_control if item.key == "risk.duplicates") == 1
    assert any(item.category == "群风控" and "链接白名单 已配置" in item.detail for item in summary.risk_details)
    assert any(item.category == "任务限速" and "每小时 12" in item.detail for item in summary.risk_details)


def test_rule_center_summary_reports_rule_conflicts_and_missing_bindings():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgGroup(id=9, tenant_id=1, tg_peer_id="-1009", title="目标群", auth_status="已授权运营"))
        session.add(TgAccount(id=11, tenant_id=1, display_name="发送号", phone_masked="11", status="在线"))
        session.add(RuleSet(id=31, tenant_id=1, name="未发布规则", status="active", active_version_id=None))
        session.add(
            Task(
                id="relay-missing-rule",
                tenant_id=1,
                name="绑定缺失版本",
                type="group_relay",
                status="running",
                type_config={"rule_set_id": 31, "rule_set_version_id": 999, "source_groups": [{"group_id": 7, "is_active": True}]},
            )
        )
        now_value = datetime.now(UTC).replace(tzinfo=None)
        previous_value = now_value - timedelta(days=8)
        session.add_all(
            [
                RuleSetVersion(
                    id=32,
                    tenant_id=1,
                    rule_set_id=31,
                    version=1,
                    status="published",
                    filters={},
                    transforms={},
                    routing={},
                    account_strategy={},
                    retry_policy={},
                    rate_limits={},
                    created_by="tester",
                    published_by="tester",
                ),
                Action(
                    id="rule-action-success",
                    tenant_id=1,
                    task_id="relay-missing-rule",
                    task_type="group_relay",
                    action_type="send_message",
                    status="success",
                    account_id=11,
                    created_at=now_value,
                    scheduled_at=now_value,
                    executed_at=now_value,
                    payload={"rule_set_id": 31, "rule_set_version_id": 32, "group_id": 9, "original_text": "公告：今晚活动"},
                ),
                Action(
                    id="rule-action-failed",
                    tenant_id=1,
                    task_id="relay-missing-rule",
                    task_type="group_relay",
                    action_type="send_message",
                    status="failed",
                    account_id=11,
                    created_at=now_value,
                    scheduled_at=now_value,
                    payload={"rule_set_id": 31, "rule_set_version_id": 32, "group_id": 9, "original_text": "公告：活动延期"},
                ),
                Action(
                    id="rule-action-previous",
                    tenant_id=1,
                    task_id="relay-missing-rule",
                    task_type="group_relay",
                    action_type="send_message",
                    status="success",
                    account_id=11,
                    created_at=previous_value,
                    executed_at=previous_value,
                    scheduled_at=previous_value,
                    payload={"rule_set_id": 31, "rule_set_version_id": 32, "group_id": 9, "original_text": "公告：上周活动"},
                ),
            ]
        )
        session.commit()

        summary = rule_center_summary(session, 1)

    keys = {item.key for item in summary.conflicts}
    assert "rule-set-no-active:31" in keys
    assert "relay-missing-rule-version:relay-missing-rule" in keys
    metric = summary.execution_metrics[0]
    assert metric.rule_set_id == 31
    assert metric.rule_set_version_id == 32
    assert metric.action_count == 3
    assert metric.success_count == 2
    assert metric.failed_count == 1
    assert metric.task_count == 1
    assert summary.target_metrics[0].name == "目标群"
    assert summary.target_metrics[0].action_count == 3
    assert summary.account_metrics[0].name == "发送号"
    assert summary.account_metrics[0].failed_count == 1
    assert summary.keyword_rule_count == 0
    assert summary.keyword_metrics == []
    today_trend = next(item for item in summary.trend_metrics if item.date == datetime.now(UTC).date().isoformat())
    assert today_trend.action_count == 2
    assert today_trend.success_count == 1
    assert today_trend.failed_count == 1
    conversion = summary.conversion_metrics[0]
    assert conversion.current_action_count == 2
    assert conversion.current_success_rate == 50.0
    assert conversion.previous_action_count == 1
    assert conversion.previous_success_rate == 100.0
    assert conversion.success_rate_delta == -50.0
    cross = summary.cross_metrics[0]
    assert cross.rule_set_version_id == 32
    assert cross.target_name == "目标群"
    assert cross.account_name == "发送号"
    assert cross.action_count == 3
    assert cross.success_rate == 66.7


def test_rule_tester_previews_transform_routing_accounts_and_rate_limits():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add_all(
            [
                TgAccount(id=101, tenant_id=1, display_name="账号A", phone_masked="101", status="在线", health_score=100),
                TgAccount(id=102, tenant_id=1, display_name="账号B", phone_masked="102", status="在线", health_score=90),
                TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="源群", auth_status="已授权运营", can_send=True),
                TgGroup(id=9, tenant_id=1, tg_peer_id="-1009", title="目标群", auth_status="已授权运营", can_send=True),
                TgGroupAccount(id=901, tenant_id=1, group_id=9, account_id=101, can_send=True),
                TgGroupAccount(id=902, tenant_id=1, group_id=9, account_id=102, can_send=True),
                RuleSet(id=31, tenant_id=1, name="转发规则", status="active", active_version_id=32),
                RuleSetVersion(
                    id=32,
                    tenant_id=1,
                    rule_set_id=31,
                    version=1,
                    status="published",
                    filters={
                        "keyword_whitelist": ["公告"],
                        "keyword_blacklist": ["禁止"],
                        "expression": {
                            "mode": "all",
                            "conditions": [
                                {"field": "content", "operator": "not_contains", "value": ["敏感"]},
                                {"field": "message_type", "operator": "in", "value": ["text"]},
                            ],
                        },
                    },
                    transforms={"prefix": "[转发] ", "keyword_replacements": {"旧词": "新词"}},
                    routing={"routes": [{"source_group_ids": [7], "keywords": ["公告"], "target_group_ids": [9]}]},
                    account_strategy={"mode": "target_sticky"},
                    retry_policy={},
                    rate_limits={"per_target_per_hour": 12, "cooldown_seconds": 30},
                    created_by="tester",
                    published_by="tester",
                ),
            ]
        )
        session.commit()

        result = preview_rules(session, 1, "公告 旧词 内容", rule_set_version_id=32, source_group_id=7)
        blocked = preview_rules(session, 1, "普通内容", rule_set_version_id=32, source_group_id=7)
        blocked_expression = preview_rules(session, 1, "公告 敏感 内容", rule_set_version_id=32, source_group_id=7)

    assert result.filter_passed is True
    assert result.transformed_text == "[转发] 公告 新词 内容"
    assert result.rule_set_name == "转发规则"
    assert result.target_routes[0].group_id == 9
    assert result.target_routes[0].can_send_account_count == 2
    assert "目标群" in result.target_summary
    assert "每目标每小时=12" in result.rate_limit_summary
    assert blocked.filter_passed is False
    assert "未命中白名单关键词" in blocked.filter_reason
    assert blocked_expression.filter_passed is False
    assert "组合条件未通过" in blocked_expression.filter_reason


def test_overview_counts_new_task_center_tasks_not_legacy_campaigns():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        now_value = _now()
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(OperationTarget(id=21, tenant_id=1, target_type="group", tg_peer_id="-10021", title="目标群"))
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="监听群", listener_last_error="poll failed"))
        session.add(Task(id="task-overview", tenant_id=1, name="新版任务", type="group_ai_chat", status="running"))
        session.add(Task(id="task-overview-failed", tenant_id=1, name="失败任务", type="group_relay", status="failed"))
        session.add(Action(id="action-overview-pending", tenant_id=1, task_id="task-overview", task_type="group_ai_chat", action_type="send_message", status="pending"))
        session.add(Action(id="action-overview-failed", tenant_id=1, task_id="task-overview-failed", task_type="group_relay", action_type="send_message", status="failed", executed_at=now_value))
        session.add(Action(id="action-overview-sent", tenant_id=1, task_id="task-overview", task_type="group_ai_chat", action_type="send_message", status="success", executed_at=now_value))
        session.add(Action(id="action-overview-like", tenant_id=1, task_id="task-overview", task_type="channel_like", action_type="like_message", status="success", executed_at=now_value))
        session.add(Action(id="action-overview-comment", tenant_id=1, task_id="task-overview", task_type="channel_comment", action_type="post_comment", status="success", executed_at=now_value))
        session.add(RuleSet(id=9, tenant_id=1, name="新版规则", status="active"))
        session.commit()

        overview = build_overview(session, 1)

    assert overview["totals"]["tasks"] == 2
    assert overview["totals"]["campaigns"] == 2
    assert overview["totals"]["targets"] == 1
    assert overview["totals"]["rules"] == 1
    assert overview["queue"]["running_tasks"] == 1
    assert overview["queue"]["failed_tasks"] == 1
    assert overview["queue"]["pending_actions"] == 1
    assert overview["queue"]["failed_actions"] == 1
    assert overview["queue"]["listener_errors"] == 1
    assert len(overview["activity_24h"]) == 24
    current_hour = now_value.replace(minute=0, second=0, microsecond=0).strftime("%H:00")
    current_bucket = next(item for item in overview["activity_24h"] if item["hour"] == current_hour)
    assert current_bucket["sent_messages"] == 1
    assert current_bucket["likes"] == 1
    assert current_bucket["comments"] == 1
    assert current_bucket["success_rate"] == 75.0
    assert current_bucket["failure_rate"] == 25.0


def test_overview_counts_timezone_aware_action_hours(monkeypatch):
    fixed_now = datetime(2026, 6, 7, 20, 15, 30)

    class AwareActionRows:
        def all(self):
            executed_at = fixed_now.replace(tzinfo=BEIJING_TZ)
            return [(executed_at, "send_message", "success")]

    class SessionWithAwareAction:
        def execute(self, _statement):
            return AwareActionRows()

    monkeypatch.setattr("app.services.reports._now", lambda: fixed_now)

    activity = _hourly_activity_24h(SessionWithAwareAction(), 1)

    current_bucket = next(item for item in activity if item["hour"] == "20:00")
    assert current_bucket["sent_messages"] == 1
    assert current_bucket["success"] == 1
    assert current_bucket["total"] == 1


def test_planning_backlog_blocked_clears_stale_stats_when_queue_recovers(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    monkeypatch.setattr(
        "app.services.task_center.service.get_settings",
        lambda: SimpleNamespace(
            max_pending_global=10_000,
            max_pending_per_task=1_000,
            oldest_pending_age_seconds=3_600,
        ),
    )

    stale_stats = {
        "planner_backlog_blocked": True,
        "planner_backlog_blocked_at": "2026-05-31T15:23:45.755171",
        "planner_backlog_global_pending": 1887,
        "planner_backlog_task_pending": 2,
        "planner_backlog_oldest_age_seconds": 19425,
        "success_count": 12,
    }
    with Session(engine) as session:
        task = Task(
            id="task-backlog-recovered",
            tenant_id=1,
            name="AI 活跃群",
            type="group_ai_chat",
            status="running",
            stats=stale_stats,
        )
        session.add_all([Tenant(id=1, name="默认运营空间"), task])
        session.commit()

        blocked = _planning_backlog_blocked(session, task)

    assert blocked is False
    assert task.stats == {"success_count": 12}


def test_planning_backlog_ignores_unrelated_old_pending_actions(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    monkeypatch.setattr(
        "app.services.task_center.stats.get_settings",
        lambda: SimpleNamespace(
            max_pending_global=10_000,
            max_pending_per_task=1_000,
            oldest_pending_age_seconds=3_600,
        ),
        raising=False,
    )

    now_value = datetime(2026, 6, 10, 23, 0, 0)
    monkeypatch.setattr("app.services.task_center.stats._now", lambda: now_value)

    with Session(engine) as session:
        ai_task = Task(
            id="task-ai-hard-target",
            tenant_id=1,
            name="AI 活跃群",
            type="group_ai_chat",
            status="running",
            type_config={"hard_hourly_target_enabled": True, "hourly_min_messages": 300},
            stats={
                "membership_joined_count": 12,
                "hard_hourly_last_blockers": {"target_membership_pending": 288},
            },
        )
        retry_task = Task(
            id="task-admission-retry",
            tenant_id=1,
            name="重试目标准入",
            type="target_admission_retry",
            status="running",
        )
        old_pending = Action(
            id="action-old-unrelated",
            tenant_id=1,
            task_id=retry_task.id,
            task_type=retry_task.type,
            action_type="ensure_target_membership",
            status="pending",
            scheduled_at=now_value - timedelta(hours=2),
        )
        session.add_all([Tenant(id=1, name="默认运营空间"), ai_task, retry_task, old_pending])
        session.commit()

        snapshot = planner_backlog_snapshot(session, ai_task)
        blocked = _planning_backlog_blocked(session, ai_task)

    assert snapshot["blocked"] is False
    assert snapshot["global_pending"] == 1
    assert snapshot["task_pending"] == 0
    assert snapshot["oldest_age_seconds"] == 0
    assert blocked is False


def test_planning_backlog_ignores_same_task_old_membership_actions(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    monkeypatch.setattr(
        "app.services.task_center.stats.get_settings",
        lambda: SimpleNamespace(
            max_pending_global=10_000,
            max_pending_per_task=1_000,
            oldest_pending_age_seconds=3_600,
        ),
        raising=False,
    )

    now_value = datetime(2026, 6, 10, 23, 0, 0)
    monkeypatch.setattr("app.services.task_center.stats._now", lambda: now_value)

    with Session(engine) as session:
        ai_task = Task(
            id="task-ai-hard-target",
            tenant_id=1,
            name="AI 活跃群",
            type="group_ai_chat",
            status="running",
            type_config={"hard_hourly_target_enabled": True, "hourly_min_messages": 300},
            stats={
                "membership_joined_count": 12,
                "hard_hourly_last_blockers": {"target_membership_pending": 288},
            },
        )
        old_membership = Action(
            id="action-old-membership",
            tenant_id=1,
            task_id=ai_task.id,
            task_type=ai_task.type,
            action_type="ensure_target_membership",
            status="pending",
            scheduled_at=now_value - timedelta(hours=2),
        )
        session.add_all([Tenant(id=1, name="默认运营空间"), ai_task, old_membership])
        session.commit()

        snapshot = planner_backlog_snapshot(session, ai_task)
        blocked = _planning_backlog_blocked(session, ai_task)

    assert snapshot["blocked"] is False
    assert snapshot["global_pending"] == 1
    assert snapshot["task_pending"] == 0
    assert snapshot["oldest_age_seconds"] == 0
    assert blocked is False


def test_planning_backlog_allows_due_hard_hourly_deficit(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    monkeypatch.setattr(
        "app.services.task_center.stats.get_settings",
        lambda: SimpleNamespace(
            max_pending_global=1,
            max_pending_per_task=1,
            oldest_pending_age_seconds=1,
        ),
        raising=False,
    )

    now_value = datetime(2026, 6, 10, 23, 20, 0)
    monkeypatch.setattr("app.services.task_center.stats._now", lambda: now_value)
    monkeypatch.setattr("app.services.task_center.service._now", lambda: now_value)

    with Session(engine) as session:
        task = Task(
            id="task-hard-hourly-backlog",
            tenant_id=1,
            name="AI 活跃群",
            type="group_ai_chat",
            status="running",
            type_config={"hard_hourly_target_enabled": True, "hourly_min_messages": 300},
            stats={"planner_backlog_blocked": True},
        )
        pending = Action(
            id="action-hard-hourly-old",
            tenant_id=1,
            task_id=task.id,
            task_type=task.type,
            action_type="send_message",
            status="pending",
            scheduled_at=now_value - timedelta(minutes=5),
        )
        session.add_all([Tenant(id=1, name="默认运营空间"), task, pending])
        session.commit()

        snapshot = planner_backlog_snapshot(session, task)
        blocked = _planning_backlog_blocked(session, task)

    assert snapshot["blocked"] is True
    assert blocked is False
    assert "planner_backlog_blocked" not in task.stats


def test_refresh_task_stats_clears_recovered_backlog_marker(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    monkeypatch.setattr(
        "app.services.task_center.stats.get_settings",
        lambda: SimpleNamespace(
            max_pending_global=10_000,
            max_pending_per_task=1_000,
            oldest_pending_age_seconds=3_600,
        ),
        raising=False,
    )

    stale_stats = {
        "planner_backlog_blocked": True,
        "planner_backlog_global_pending": 1887,
        "success_count": 12,
    }
    with Session(engine) as session:
        task = Task(
            id="task-backlog-list-refresh",
            tenant_id=1,
            name="AI 活跃群",
            type="group_ai_chat",
            status="running",
            stats=stale_stats,
        )
        session.add_all([Tenant(id=1, name="默认运营空间"), task])
        session.commit()

        stats = refresh_task_stats(session, task)

    assert "planner_backlog_blocked" not in stats
    assert "planner_backlog_global_pending" not in stats


def test_list_tasks_keeps_stored_stats_without_action_recount(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    def fail_refresh(_session, _task):
        raise AssertionError("task list should not refresh action stats")

    monkeypatch.setattr("app.services.task_center.service.refresh_task_stats", fail_refresh)

    with Session(engine) as session:
        task = Task(
            id="task-backlog-list-payload",
            tenant_id=1,
            name="AI 活跃群",
            type="group_ai_chat",
            status="running",
            stats={
                "planner_backlog_blocked": True,
                "planner_backlog_global_pending": 1887,
            },
        )
        session.add_all([Tenant(id=1, name="默认运营空间"), task])
        session.commit()

        listed = list_tasks(session, 1, "group_ai_chat", "running")

    stats = listed[0]["stats"]
    assert stats["planner_backlog_blocked"] is True
    assert stats["planner_backlog_global_pending"] == 1887


def test_list_tasks_uses_runtime_summary_without_recounting_actions(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    def fail_refresh(_session, _task):
        raise AssertionError("task list should use runtime summaries for list counters")

    monkeypatch.setattr("app.services.task_center.service.refresh_task_stats", fail_refresh)

    with Session(engine) as session:
        task = Task(
            id="task-list-summary",
            tenant_id=1,
            name="AI 活跃群",
            type="group_ai_chat",
            status="running",
            stats={
                "hard_hourly_target_enabled": True,
                "hard_hourly_goal": 300,
                "hard_hourly_success_count": 16,
                "hard_hourly_deficit": 284,
            },
        )
        summary = TaskRuntimeSummary(
            tenant_id=1,
            task_id=task.id,
            task_status="running",
            planned_count=3755,
            success_count=2202,
            failed_count=171,
            pending_count=154,
        )
        session.add_all([Tenant(id=1, name="默认运营空间"), task, summary])
        session.commit()

        [listed] = list_tasks(session, 1, "group_ai_chat", "running")

    assert listed["stats"]["total_actions"] == 3755
    assert listed["stats"]["success_count"] == 2202
    assert listed["stats"]["failure_count"] == 171
    assert listed["stats"]["pending_count"] == 154
    assert listed["stats"]["hard_hourly_goal"] == 300
    assert listed["stats"]["hard_hourly_deficit"] == 284


def test_list_tasks_without_runtime_summary_does_not_recount_actions(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    def fail_refresh(_session, _task):
        raise AssertionError("missing runtime summary must not block task list")

    monkeypatch.setattr("app.services.task_center.service.refresh_task_stats", fail_refresh)

    with Session(engine) as session:
        task = Task(
            id="task-list-no-summary",
            tenant_id=1,
            name="AI 活跃群",
            type="group_ai_chat",
            status="running",
            stats={"success_count": 8, "hard_hourly_goal": 300, "hard_hourly_deficit": 292},
        )
        session.add_all([Tenant(id=1, name="默认运营空间"), task])
        session.commit()

        [listed] = list_tasks(session, 1, "group_ai_chat", "running")

    assert listed["stats"]["success_count"] == 8
    assert listed["stats"]["hard_hourly_goal"] == 300
    assert listed["stats"]["hard_hourly_deficit"] == 292


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
        update_operation_target(
            session,
            1,
            targets[0]["id"],
            OperationTargetUpdate(
                active_window="10:00-22:00",
                daily_limit=88,
                account_cooldown_seconds=240,
                group_cooldown_seconds=90,
                banned_words="spam,广告",
                link_whitelist="example.com",
                require_review=False,
            ),
            "pytest",
        )
        account_detail = update_operation_target_account_policy(
            session,
            1,
            targets[0]["id"],
            11,
            OperationTargetAccountUpdate(can_send=False, is_listener=True, permission_label="风控观察"),
            "pytest",
        )
        detail = operation_target_detail(session, 1, targets[0]["id"])

    assert targets[0]["linked_group_id"] == 7
    assert targets[0]["available_send_account_count"] == 1
    assert targets[0]["listener_account_count"] == 1
    assert targets[0]["can_listen"] is True
    assert detail["linked_group"]["active_window"] == "10:00-22:00"
    assert detail["linked_group"]["daily_limit"] == 88
    assert detail["linked_group"]["account_cooldown_seconds"] == 240
    assert detail["linked_group"]["group_cooldown_seconds"] == 90
    assert detail["linked_group"]["banned_words"] == "spam,广告"
    assert detail["linked_group"]["link_whitelist"] == "example.com"
    assert detail["linked_group"]["require_review"] is False
    account_row = next(item for item in account_detail["accounts"] if item["id"] == 11)
    assert account_row["can_send"] is False
    assert account_row["is_listener"] is True
    assert account_row["permission_label"] == "风控观察"


def test_operation_target_admission_retry_queues_failed_accounts_and_audits(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("admission retry must queue membership actions")

    monkeypatch.setattr("app.services.operations.gateway.list_groups", fail_if_called)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add_all(
            [
                OperationTarget(id=21, tenant_id=1, target_type="group", tg_peer_id="-1001", title="运营群", can_send=False, auth_status="只读"),
                TgGroup(id=7, tenant_id=1, tg_peer_id="-1001", title="运营群", auth_status="只读", can_send=False),
                TgAccount(id=11, tenant_id=1, display_name="解除限制号", phone_masked="+861***0011", status=AccountStatus.ACTIVE.value, session_ciphertext="session-11"),
                TgAccount(id=12, tenant_id=1, display_name="未入群号", phone_masked="+861***0012", status=AccountStatus.ACTIVE.value, session_ciphertext="session-12"),
                TgAccount(id=13, tenant_id=1, display_name="仍禁言号", phone_masked="+861***0013", status=AccountStatus.ACTIVE.value, session_ciphertext="session-13"),
                TgGroupAccount(tenant_id=1, group_id=7, account_id=11, can_send=False, permission_label="禁言"),
                TgGroupAccount(tenant_id=1, group_id=7, account_id=12, can_send=False, permission_label="未加入或不可见"),
                TgGroupAccount(tenant_id=1, group_id=7, account_id=13, can_send=False, permission_label="禁言"),
            ]
        )
        session.commit()

        before_detail = operation_target_detail(session, 1, 21)
        result = retry_operation_target_admission(
            session,
            1,
            21,
            OperationTargetAdmissionRetryRequest(reason="管理员已解除限制", account_ids=[11, 12, 13]),
            "pytest",
        )
        audit_row = session.scalar(select(AuditLog).where(AuditLog.action == "重试目标准入"))
        refreshed_target = session.get(OperationTarget, 21)
        queued_actions = list(session.scalars(select(Action).where(Action.action_type == "ensure_target_membership")))

    before_failed = {item["id"]: item for item in before_detail["accounts"]}
    assert before_failed[11]["admission_status"] == "failed"
    assert before_failed[11]["admission_failure_reason"] == "禁言"
    assert result["admission_retry"]["mode"] == "queued"
    assert result["admission_retry"]["retried_account_count"] == 3
    assert result["admission_retry"]["queued_action_count"] == 3
    assert result["admission_retry"]["recovered_account_count"] == 0
    assert result["admission_retry"]["failed_account_count"] == 0
    assert result["stats"]["admission_failed_accounts"] == 3
    assert len(queued_actions) == 3
    assert {action.status for action in queued_actions} == {"pending"}
    rows = {item["id"]: item for item in result["accounts"]}
    assert rows[11]["can_send"] is False
    assert rows[11]["admission_status"] == "failed"
    assert rows[12]["can_send"] is False
    assert rows[12]["admission_status"] == "failed"
    assert rows[12]["admission_failure_reason"] == "未加入或不可见"
    assert rows[13]["can_send"] is False
    assert rows[13]["admission_status"] == "failed"
    assert rows[13]["admission_failure_reason"] == "禁言"
    assert refreshed_target and refreshed_target.can_send is False
    assert audit_row is not None
    assert "reason=管理员已解除限制" in audit_row.detail
    assert "queued=3" in audit_row.detail
    assert "failed=0" in audit_row.detail


def test_operation_target_bulk_admission_retry_queues_membership_actions_without_gateway_calls(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("bulk retry must not call Telegram in the HTTP request")

    monkeypatch.setattr("app.services.operations.gateway.list_groups", fail_if_called)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(OperationTarget(id=21, tenant_id=1, target_type="group", tg_peer_id="-1001", title="运营群", can_send=False, auth_status="只读"))
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1001", title="运营群", auth_status="只读", can_send=False))
        for account_id in range(1, 320):
            session.add(TgAccount(id=account_id, tenant_id=1, display_name=f"账号{account_id}", phone_masked=str(account_id), status=AccountStatus.ACTIVE.value, session_ciphertext=f"session-{account_id}"))
            session.add(TgGroupAccount(tenant_id=1, group_id=7, account_id=account_id, can_send=False, permission_label="账号无权限"))
        session.commit()

        result = retry_operation_target_admission(
            session,
            1,
            21,
            OperationTargetAdmissionRetryRequest(reason="批量重查准入", account_ids=list(range(1, 320))),
            "pytest",
        )
        queued_actions = list(session.scalars(select(Action).where(Action.action_type == "ensure_target_membership")))
        audit_row = session.scalar(select(AuditLog).where(AuditLog.action == "重试目标准入"))

    assert result["admission_retry"]["mode"] == "queued"
    assert result["admission_retry"]["queued_action_count"] == 319
    assert result["admission_retry"]["retried_account_count"] == 319
    assert result["admission_retry"]["recovered_account_count"] == 0
    assert len(queued_actions) == 319
    assert {action.status for action in queued_actions} == {"pending"}
    assert {action.payload["channel_target_id"] for action in queued_actions} == {21}
    assert {action.payload["require_send"] for action in queued_actions} == {True}
    assert audit_row is not None
    assert "queued=319" in audit_row.detail


def test_verification_group_restriction_batch_queues_target_admission_retry(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("verification batch retry must not call Telegram in the HTTP request")

    monkeypatch.setattr("app.services.verification.gateway.approve_group_verification_messages", fail_if_called)
    monkeypatch.setattr("app.services.operations.gateway.list_groups", fail_if_called)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(OperationTarget(id=21, tenant_id=1, target_type="group", tg_peer_id="-1001", title="运营群", can_send=False, auth_status="只读"))
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1001", title="运营群", auth_status="只读", can_send=False))
        session.add(VerificationTask(id=693, tenant_id=1, account_id=1, group_id=7, verification_type="群发言权限", target_peer_id="-1001", target_display="运营群", status="需人工处理"))
        for account_id in range(1, 320):
            session.add(TgAccount(id=account_id, tenant_id=1, display_name=f"账号{account_id}", phone_masked=str(account_id), status=AccountStatus.ACTIVE.value, session_ciphertext=f"session-{account_id}"))
            session.add(TgGroupAccount(tenant_id=1, group_id=7, account_id=account_id, can_send=False, permission_label="账号无权限"))
        session.commit()

        result = resolve_group_restriction_batch(session, 693, "pytest")
        queued_actions = list(session.scalars(select(Action).where(Action.action_type == "ensure_target_membership")))
        audit_row = session.scalar(select(AuditLog).where(AuditLog.action == "重试目标准入"))

    assert result.approval_status == "已转后台重查"
    assert result.checked_count == 319
    assert result.blocked_count == 319
    assert result.restored_count == 0
    assert "已提交后台目标准入重查 319 个动作" in result.message
    assert len(queued_actions) == 319
    assert {action.status for action in queued_actions} == {"pending"}
    assert {action.payload["channel_target_id"] for action in queued_actions} == {21}
    assert audit_row is not None
    assert "queued=319" in audit_row.detail


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
    monkeypatch.setattr(worker, "drain_account_security_batches", lambda *args, **kwargs: 0)
    monkeypatch.setattr(worker, "drain_group_listeners", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("legacy group listener must be opt-in")))
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


def test_task_center_pre_send_validation_blocks_internal_prompts(monkeypatch):
    from app.services.task_center import dispatcher

    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *args, **kwargs: object())
    monkeypatch.setattr(dispatcher.gateway, "send_message", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("internal prompt must not call TG")))

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add_all(
            [
                TgAccount(id=11, tenant_id=1, display_name="发送号", phone_masked="+861***0011", status="在线"),
                TgGroup(id=7, tenant_id=1, tg_peer_id="-1001", title="运营群", auth_status="已授权运营", can_send=True, banned_words=""),
                TgGroupAccount(tenant_id=1, group_id=7, account_id=11, can_send=True),
                Task(id="task-internal-prompt", tenant_id=1, name="提示词拦截", type="group_ai_chat", status="running"),
                Action(
                    id="action-internal-prompt",
                    tenant_id=1,
                    task_id="task-internal-prompt",
                    task_type="group_ai_chat",
                    action_type="send_message",
                    account_id=11,
                    status="pending",
                    payload={
                        "group_id": 7,
                        "message_text": "当前群暂无可用历史消息。请以“日常讨论”为方向，生成自然开场，不要提到系统、任务或 AI。",
                        "review_approved": True,
                    },
                    result={},
                ),
            ]
        )
        session.commit()

        action = session.get(Action, "action-internal-prompt")
        assert dispatcher.dispatch_action(session, action) is True

        assert action.status == "failed"
        assert action.result["auto_check"] == "拦截"
        assert action.result["validation_stage"] == "content_policy"
        assert "内部提示词" in action.result["error_message"]


def test_task_center_pre_send_validation_blocks_template_rewrite_noise(monkeypatch):
    from app.services.task_center import dispatcher

    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *args, **kwargs: object())
    monkeypatch.setattr(dispatcher.gateway, "send_message", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("template content must not call TG")))

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add_all(
            [
                TgAccount(id=11, tenant_id=1, display_name="发送号", phone_masked="+861***0011", status="在线"),
                TgGroup(id=7, tenant_id=1, tg_peer_id="-1001", title="运营群", auth_status="已授权运营", can_send=True, banned_words=""),
                TgGroupAccount(tenant_id=1, group_id=7, account_id=11, can_send=True),
                Task(id="task-template-noise", tenant_id=1, name="模板拦截", type="group_relay", status="running"),
                Action(
                    id="action-template-noise",
                    tenant_id=1,
                    task_id="task-template-noise",
                    task_type="group_relay",
                    action_type="send_message",
                    account_id=11,
                    status="pending",
                    payload={
                        "group_id": 7,
                        "message_text": "顺着这个话题说，点击底部按钮可以打开更多功能，有经验的朋友也可以补充下。",
                        "review_approved": True,
                    },
                    result={},
                ),
            ]
        )
        session.commit()

        action = session.get(Action, "action-template-noise")
        assert dispatcher.dispatch_action(session, action) is True

        assert action.status == "failed"
        assert action.result["auto_check"] == "拦截"
        assert action.result["validation_stage"] == "content_policy"
        assert "模板化生成内容" in action.result["error_message"]


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
                Action(id="action-executing", tenant_id=1, task_id="task-reset", task_type="group_relay", action_type="send_message", status="executing", result={}),
                Action(id="action-claiming", tenant_id=1, task_id="task-reset", task_type="group_relay", action_type="send_message", status="claiming", result={}),
                Action(id="action-retryable", tenant_id=1, task_id="task-reset", task_type="group_relay", action_type="send_message", status="retryable_failed", result={}),
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
    assert actions == {
        "action-success": "success",
        "action-failed": "failed",
        "action-executing": "skipped",
        "action-claiming": "skipped",
        "action-retryable": "skipped",
    }
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
                TgAccount(id=42, tenant_id=1, display_name="审计账号", phone_masked="+861***0042", phone_ciphertext=encrypt_secret("+8613800000042"), status="在线"),
                AuditLog(tenant_id=1, actor="admin", action="同步TG账号", target_type="tg_account", target_id="42", detail="contacts=3"),
                AuditLog(tenant_id=2, actor="admin", action="启动任务中心任务", target_type="task", target_id="task-2", detail="other tenant"),
            ]
        )
        session.commit()

        assert [item.target_id for item in filter_audit_logs(session, 1, task_id="task-1")] == ["99", "task-1"]
        assert [item.target_id for item in filter_audit_logs(session, 1, account_id="42")] == ["42"]
        assert [item.target_id for item in filter_audit_logs(session, 1, status="failed")] == ["99"]
        assert [item.target_id for item in filter_audit_logs(session, 1, keyword="group_relay")] == ["task-1"]
        enriched = [{"id": item.id, "tenant_id": item.tenant_id, "actor": item.actor, "action": item.action, "target_type": item.target_type, "target_id": item.target_id, "account_display_name": "审计账号", "account_phone_number": "+8613800000042", "detail": item.detail, "ip_address": item.ip_address, "created_at": item.created_at} for item in filter_audit_logs(session, 1, account_id="42")]
        csv_text = audit_logs_csv(enriched)
        assert "id,tenant_id,actor,action,target_type,target_id,account_display_name,account_phone_number,detail,ip_address,created_at" in csv_text
        assert "+8613800000042" in csv_text
        csv_text = audit_logs_csv(filter_audit_logs(session, 1, task_id="task-1"))
        assert "执行消息发送失败" in csv_text


def test_message_send_failure_rolls_up_operation_issue(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    SessionLocalForTest = sessionmaker(engine, future=True)

    monkeypatch.setattr("app.services.messages.credentials_for_account", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(
        "app.services.messages.gateway.send_message",
        lambda *_args, **_kwargs: SendResult(ok=False, failure_type=FailureType.GROUP_PERMISSION_DENIED.value, detail="群当前不可发送"),
    )

    with SessionLocalForTest() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add_all(
            [
                TgAccount(id=11, tenant_id=1, display_name="发送号", phone_masked="+861***0011", status=AccountStatus.ACTIVE.value, session_ciphertext="session-11"),
                TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="运营群", auth_status="已授权运营", can_send=True, account_cooldown_seconds=0, group_cooldown_seconds=0),
                TgGroupAccount(tenant_id=1, group_id=7, account_id=11, can_send=True),
                OperationTarget(id=21, tenant_id=1, target_type="group", tg_peer_id="-1007", title="运营群", can_send=True, auth_status="已授权运营"),
                MessageTask(
                    id=99,
                    tenant_id=1,
                    group_id=7,
                    account_id=11,
                    preferred_account_id=11,
                    content="hello",
                    target_type="group",
                    target_peer_id="-1007",
                    target_display="运营群",
                    status=TaskStatus.QUEUED.value,
                    idempotency_key="pytest-message-rollup",
                    scheduled_at=_now() - timedelta(seconds=1),
                ),
                MessageTask(
                    id=100,
                    tenant_id=1,
                    group_id=7,
                    account_id=11,
                    preferred_account_id=11,
                    content="hello again",
                    target_type="group",
                    target_peer_id="-1007",
                    target_display="运营群",
                    status=TaskStatus.QUEUED.value,
                    idempotency_key="pytest-message-rollup-2",
                    scheduled_at=_now() - timedelta(seconds=1),
                ),
            ]
        )
        session.commit()

    dispatched = dispatch_task(SessionLocalForTest, 99)
    second_dispatched = dispatch_task(SessionLocalForTest, 100)

    with SessionLocalForTest() as session:
        issue = session.scalar(select(OperationIssue).where(OperationIssue.target_id == 21, OperationIssue.status == "open"))
        sources = list(session.scalars(select(OperationIssueSource).where(OperationIssueSource.source_type == "message_task").order_by(OperationIssueSource.source_id.asc())))
        target_summary = session.scalar(select(TargetRuntimeSummary).where(TargetRuntimeSummary.target_id == 21))

    assert dispatched.status == TaskStatus.FAILED.value
    assert second_dispatched.status == TaskStatus.FAILED.value
    assert getattr(dispatched, "operation_issue_rolled_up") is True
    assert getattr(dispatched, "operation_issue_status") == "open"
    assert issue is not None
    assert issue.source_task_id == "message_task:100"
    assert issue.failure_type == FailureType.GROUP_PERMISSION_DENIED.value
    assert issue.failure_reason == "群当前不可发送"
    assert issue.target_id == 21
    assert issue.affected_account_ids == [11]
    assert issue.return_to["page"] == "message-sending"
    assert issue.return_to["message_task_id"] == 100
    assert sorted(source.source_id for source in sources) == ["100", "99"]
    assert all(source.summary["target_display"] == "运营群" for source in sources)
    assert target_summary is not None
    assert target_summary.open_issue_count == 1
    with SessionLocalForTest() as session:
        listed = filter_tasks(session, 1, 1, 10, None, None)
    listed_by_id = {item.id: item for item in listed}
    assert listed_by_id[99].operation_issue_id == issue.id
    assert listed_by_id[99].operation_issue_status == "open"
    assert listed_by_id[99].operation_issue_rolled_up is True
    assert listed_by_id[100].operation_issue_id == issue.id
    assert listed_by_id[100].operation_issue_status == "open"
    assert listed_by_id[100].operation_issue_rolled_up is True

    monkeypatch.setattr(
        "app.services.messages.gateway.send_message",
        lambda *_args, **_kwargs: SendResult(ok=True, remote_message_id="remote-99"),
    )
    retried = retry_task(SessionLocalForTest, 100, "pytest", True)
    with SessionLocalForTest() as session:
        still_open_issue = session.get(OperationIssue, issue.id)
        still_open_summary = session.scalar(select(TargetRuntimeSummary).where(TargetRuntimeSummary.target_id == 21))

    assert retried.status == TaskStatus.SENT.value
    assert still_open_issue is not None
    assert still_open_issue.status == "open"
    assert still_open_summary is not None
    assert still_open_summary.open_issue_count == 1
    with SessionLocalForTest() as session:
        listed_after_partial_recovery = {item.id: item for item in filter_tasks(session, 1, 1, 10, None, None)}
    assert listed_after_partial_recovery[99].operation_issue_id == issue.id
    assert listed_after_partial_recovery[99].operation_issue_status == "open"
    assert listed_after_partial_recovery[99].operation_issue_rolled_up is True

    retried_original = retry_task(SessionLocalForTest, 99, "pytest", True)
    with SessionLocalForTest() as session:
        resolved_issue = session.get(OperationIssue, issue.id)
        resolved_summary = session.scalar(select(TargetRuntimeSummary).where(TargetRuntimeSummary.target_id == 21))

    assert retried_original.status == TaskStatus.SENT.value
    assert resolved_issue is not None
    assert resolved_issue.status == "resolved"
    assert resolved_issue.summary["auto_resolved"] is True
    assert resolved_summary is not None
    assert resolved_summary.open_issue_count == 0

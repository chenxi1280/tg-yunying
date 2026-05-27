from contextlib import contextmanager
from datetime import timedelta
import socket

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import AccountProxy, AccountRuntimeSummary, AccountStatus, Action, AuditLog, FailureType, MessageTask, MessageTaskAttempt, ProxyAlert, SchedulingSetting, Task, TaskStatus, Tenant, TgAccount, TgAccountSecuritySnapshot
from app.schemas import MessageSendTaskCreate
from app.schemas.risk_control import ProxyBindingRequest, RiskPreflightRequest
from app.services._common import _now
from app.services.messages import create_message_send_task
from app.services.risk_control import bind_account_proxy, check_account_proxy, disable_account_proxy, risk_control_summary, risk_preflight, update_proxy_alert_status


@contextmanager
def listening_port():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    try:
        yield listener.getsockname()[1]
    finally:
        listener.close()


def test_risk_control_summary_separates_account_lifecycle_from_runtime_policy():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now = _now()

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(SchedulingSetting(tenant_id=1, default_account_hour_limit=1, default_account_day_limit=5, default_account_cooldown_seconds=0))
        session.add_all(
            [
                TgAccount(id=11, tenant_id=1, display_name="可用账号", phone_masked="11", status=AccountStatus.ACTIVE.value, health_score=96),
                TgAccount(id=12, tenant_id=1, display_name="待登录账号", phone_masked="12", status=AccountStatus.WAITING_CODE.value, health_score=90),
                TgAccount(id=13, tenant_id=1, display_name="容量账号", phone_masked="13", status=AccountStatus.ACTIVE.value, health_score=92),
                TgAccount(id=14, tenant_id=1, display_name="受限账号", phone_masked="14", status=AccountStatus.LIMITED.value, health_score=88),
            ]
        )
        session.add(Task(id="task-13", tenant_id=1, name="容量任务", type="channel_like", status="running"))
        session.add(
            Action(
                id="action-13",
                tenant_id=1,
                task_id="task-13",
                task_type="channel_like",
                action_type="like_message",
                account_id=13,
                status="pending",
                scheduled_at=now,
            )
        )
        session.add(
            MessageTask(
                id=21,
                tenant_id=1,
                account_id=14,
                content="失败消息",
                status=TaskStatus.FAILED.value,
                idempotency_key="failed-21",
                failure_type=FailureType.FLOOD_WAIT.value,
                failure_detail="FloodWait 120 秒",
                scheduled_at=now,
            )
        )
        session.commit()

        summary = risk_control_summary(session, 1)

    accounts = {item["account_id"]: item for item in summary["account_scores"]}
    assert accounts[11]["risk_level"] == "A"
    assert accounts[11]["can_join_task"] is True
    assert accounts[12]["risk_level"] == "E"
    assert accounts[12]["blocked_reason"] == AccountStatus.WAITING_CODE.value
    assert accounts[13]["risk_level"] == "D"
    assert accounts[13]["current_policy"] == "转派或延后"
    assert accounts[13]["can_join_task"] is False

    queue_types = {item["item_type"] for item in summary["disposition_queue"]}
    assert {"待完成登录", "账号受限", "账号容量受限"}.issubset(queue_types)
    assert any(item["policy"] == FailureType.FLOOD_WAIT.value for item in summary["hit_records"])
    assert summary["overview"]["current_level"] in {"收紧", "暂停"}


def test_risk_control_summary_reuses_existing_scheduling_setting():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgAccount(id=11, tenant_id=1, display_name="可用账号", phone_masked="11", status=AccountStatus.ACTIVE.value, health_score=86))
        session.commit()

        summary = risk_control_summary(session, 1)
        setting_count = session.scalar(select(func.count(SchedulingSetting.id)).where(SchedulingSetting.tenant_id == 1))

    assert summary["global_policy"]["default_retry_backoff"] == "exponential"
    assert summary["account_scores"][0]["current_policy"] == "标准节奏"
    assert setting_count == 1


def test_risk_control_summary_batches_account_capacity_queries():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    statements: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def _capture_sql(_conn, _cursor, statement, _parameters, _context, _executemany):  # noqa: ANN001
        statements.append(statement)

    now = _now()
    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(SchedulingSetting(tenant_id=1, default_account_cooldown_seconds=300))
        for account_id in range(1, 26):
            session.add(TgAccount(id=account_id, tenant_id=1, display_name=f"账号{account_id}", phone_masked=str(account_id), status=AccountStatus.ACTIVE.value, health_score=90))
            session.add(Action(id=f"action-{account_id}", tenant_id=1, task_id="task", task_type="group_ai_chat", action_type="send_message", account_id=account_id, status="success", scheduled_at=now - timedelta(minutes=1)))
        session.commit()
        statements.clear()

        summary = risk_control_summary(session, 1)

    assert len(summary["account_scores"]) == 25
    capacity_queries = [
        statement for statement in statements
        if "max(coalesce" in statement.lower() and ("FROM actions" in statement or "FROM message_tasks" in statement)
    ]
    assert len(capacity_queries) <= 4


def test_risk_control_summary_includes_policy_audit_records():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgAccount(id=11, tenant_id=1, display_name="可用账号", phone_masked="11", status=AccountStatus.ACTIVE.value, health_score=86))
        session.add_all(
            [
                AuditLog(
                    tenant_id=1,
                    actor="admin",
                    action="更新风控全局策略",
                    target_type="risk_global_policy",
                    target_id="1",
                    detail="trace_id=risk-audit-1; reason=收紧策略",
                ),
                AuditLog(
                    tenant_id=1,
                    actor="admin",
                    action="绑定账号本地代理",
                    target_type="tg_account",
                    target_id="11",
                    detail="trace_id=risk-audit-2; reason=切换代理",
                ),
                AuditLog(
                    tenant_id=1,
                    actor="admin",
                    action="普通审计",
                    target_type="task",
                    target_id="task-1",
                    detail="不属于风控策略审计",
                ),
            ]
        )
        session.commit()

        summary = risk_control_summary(session, 1)

    audits = summary["policy_audits"]
    assert [item["action"] for item in audits] == ["绑定账号本地代理", "更新风控全局策略"]
    assert audits[0]["target_type"] == "tg_account"
    assert audits[0]["target_label"] == "账号代理绑定"
    assert "risk-audit-2" in audits[0]["detail"]
    assert "普通审计" not in {item["action"] for item in audits}


def test_low_health_score_is_visible_in_score_reasons():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgAccount(id=11, tenant_id=1, display_name="低分账号", phone_masked="11", status=AccountStatus.ACTIVE.value, health_score=42))
        session.commit()

        summary = risk_control_summary(session, 1)

    score = summary["account_scores"][0]
    assert score["risk_level"] == "D"
    assert score["blocked_reason"] == "健康分低于任务准入线"
    assert "健康分 42.0 低于任务准入线 55" in score["score_reasons"]


def test_risk_control_summary_uses_account_runtime_summary_as_health_source():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgAccount(id=11, tenant_id=1, display_name="运行汇总账号", phone_masked="11", status=AccountStatus.ACTIVE.value, health_score=96))
        session.add(
            AccountRuntimeSummary(
                tenant_id=1,
                account_id=11,
                send_available=True,
                listen_available=True,
                join_available=True,
                comment_available=True,
                profile_available=True,
                code_read_available=True,
                remaining_capacity=88,
                health_score=42,
                risk_level="D",
                score_reasons=["运行读模型健康分低于准入线"],
            )
        )
        session.commit()

        summary = risk_control_summary(session, 1)

    score = summary["account_scores"][0]
    assert score["health_score"] == 42
    assert score["risk_level"] == "D"
    assert "运行读模型健康分低于准入线" in score["score_reasons"]


def test_target_permission_failure_is_non_score_reason():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now = _now()

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgAccount(id=11, tenant_id=1, display_name="评论账号", phone_masked="11", status=AccountStatus.ACTIVE.value, health_score=92))
        session.add(Task(id="task-comment", tenant_id=1, name="频道评论", type="channel_comment", status="running"))
        session.add(
            Action(
                id="action-comment-denied",
                tenant_id=1,
                task_id="task-comment",
                task_type="channel_comment",
                action_type="post_comment",
                account_id=11,
                status="failed",
                scheduled_at=now,
                executed_at=now,
                result={
                    "failure_type": FailureType.COMMENT_UNAVAILABLE.value,
                    "error_message": "无评论权限，未通过群限制发言",
                },
            )
        )
        session.commit()

        summary = risk_control_summary(session, 1)

    account = summary["account_scores"][0]
    assert account["risk_level"] == "A"
    assert account["recent_risk"] == ""
    assert not any("无评论权限" in reason for reason in account["score_reasons"])
    assert any("无评论权限" in reason for reason in account["non_score_reasons"])

    hit = summary["hit_records"][0]
    assert hit["policy"] == FailureType.COMMENT_UNAVAILABLE.value
    assert hit["impact_scope"] == "target"
    assert hit["affects_health_score"] is False
    assert hit["suggested_entry"] == "运营目标 / 账号目标能力"
    assert any(item["key"] == f"hit:{hit['key']}" for item in summary["disposition_queue"])


def test_message_attempt_task_failure_is_hit_but_not_account_score():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now = _now()

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgAccount(id=12, tenant_id=1, display_name="消息账号", phone_masked="12", status=AccountStatus.ACTIVE.value, health_score=94))
        session.add(
            MessageTask(
                id=900,
                tenant_id=1,
                target_type="group",
                target_peer_id="-100900",
                target_display="测试群",
                content="hello",
                idempotency_key="message-task-900",
                status=TaskStatus.SENDING.value,
                scheduled_at=now,
            )
        )
        session.add(
            MessageTaskAttempt(
                id=901,
                tenant_id=1,
                task_id=900,
                account_id=12,
                status="failed",
                failure_type=FailureType.UNKNOWN.value,
                detail="任务配置错误，缺少素材",
                created_at=now,
            )
        )
        session.commit()

        summary = risk_control_summary(session, 1)

    account = summary["account_scores"][0]
    assert account["health_score"] == 94
    assert account["risk_level"] == "A"
    assert account["recent_risk"] == ""
    assert not any("任务配置错误" in reason for reason in account["score_reasons"])
    assert "任务配置错误，缺少素材" in "；".join(account["non_score_reasons"])

    hit = next(item for item in summary["hit_records"] if item["key"] == "message-attempt:901")
    assert hit["impact_scope"] == "task"
    assert hit["affects_health_score"] is False
    assert hit["severity"] == "warning"
    assert any(item["key"] == f"hit:{hit['key']}" for item in summary["disposition_queue"])


def test_group_permission_failure_is_disposition_only_not_account_score():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now = _now()

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgAccount(id=13, tenant_id=1, display_name="群权限账号", phone_masked="13", status=AccountStatus.ACTIVE.value, health_score=91))
        session.add(Task(id="task-group", tenant_id=1, name="群活跃", type="group_ai_chat", status="running"))
        session.add(
            Action(
                id="action-group-denied",
                tenant_id=1,
                task_id="task-group",
                task_type="group_ai_chat",
                action_type="send_message",
                account_id=13,
                status="failed",
                scheduled_at=now,
                executed_at=now,
                result={
                    "failure_type": FailureType.GROUP_PERMISSION_DENIED.value,
                    "blockers": ["账号未通过群限制发言"],
                    "error_message": "账号没有该目标发言权限",
                },
            )
        )
        session.commit()

        summary = risk_control_summary(session, 1)

    account = summary["account_scores"][0]
    assert account["health_score"] == 91
    assert account["risk_level"] == "A"
    assert account["recent_risk"] == ""
    assert not account["score_reasons"] or "账号未通过群限制发言" not in "；".join(account["score_reasons"])
    assert "账号没有该目标发言权限" in "；".join(account["non_score_reasons"])

    hit = summary["hit_records"][0]
    assert hit["impact_scope"] == "target"
    assert hit["affects_health_score"] is False
    assert hit["severity"] == "warning"
    assert any(item["key"] == f"hit:{hit['key']}" for item in summary["disposition_queue"])


def test_unknown_hit_record_keeps_trace_id_and_is_marked_unclassified():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now = _now()

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgAccount(id=12, tenant_id=1, display_name="未知错误账号", phone_masked="12", status=AccountStatus.ACTIVE.value, health_score=92))
        session.add(Task(id="task-unknown", tenant_id=1, name="未知错误任务", type="group_ai_chat", status="running"))
        session.add(
            Action(
                id="action-unknown",
                tenant_id=1,
                task_id="task-unknown",
                task_type="group_ai_chat",
                action_type="send_message",
                account_id=12,
                status="failed",
                scheduled_at=now,
                executed_at=now,
                result={
                    "failure_type": FailureType.UNKNOWN.value,
                    "error_message": FailureType.UNKNOWN.value,
                    "trace_id": "trace-risk-unknown",
                },
            )
        )
        session.commit()

        summary = risk_control_summary(session, 1)

    hit = summary["hit_records"][0]
    assert hit["policy"] == FailureType.UNKNOWN.value
    assert hit["detail"] != FailureType.UNKNOWN.value
    assert "待分类" in hit["detail"]
    assert "trace-risk-unknown" in hit["detail"]
    assert hit["affects_health_score"] is False


def test_proxy_binding_is_visible_in_account_score_and_preflight():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgAccount(id=11, tenant_id=1, display_name="代理账号", phone_masked="11", status=AccountStatus.ACTIVE.value, health_score=90))
        proxy = AccountProxy(id=31, tenant_id=1, name="local-1080", host="127.0.0.1", port=1080, status="healthy", alert_status="normal")
        session.add(proxy)
        session.commit()

        bind_result = bind_account_proxy(session, 1, 11, ProxyBindingRequest(proxy_id=31, change_reason="测试绑定"), "tester")
        summary = risk_control_summary(session, 1)
        preflight = risk_preflight(session, 1, RiskPreflightRequest(account_ids=[11], content_preview="正常消息"))

    account = summary["account_scores"][0]
    assert bind_result["new_proxy_id"] == 31
    assert account["proxy_id"] == 31
    assert account["proxy_local_address"] == "socks5://127.0.0.1:1080"
    assert account["can_join_task"] is True
    assert preflight["decision"] == "allow"
    assert preflight["proxy_decisions"][0]["blocks"] is False


def test_account_security_snapshot_degrades_risk_score_and_preflight():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgAccount(id=11, tenant_id=1, display_name="安全待处理账号", phone_masked="11", status=AccountStatus.ACTIVE.value, health_score=90))
        session.add(
            TgAccountSecuritySnapshot(
                tenant_id=1,
                account_id=11,
                trusted_session_status="confirmed",
                two_fa_status="missing",
                external_authorization_count=2,
                profile_status="incomplete",
            )
        )
        session.commit()

        summary = risk_control_summary(session, 1)
        preflight = risk_preflight(session, 1, RiskPreflightRequest(account_ids=[11], content_preview="正常消息"))

    account = summary["account_scores"][0]
    assert account["risk_level"] in {"C", "D"}
    assert account["trusted_session_status"] == "confirmed"
    assert account["two_fa_status"] == "missing"
    assert account["external_authorization_count"] == 2
    assert "外部登录设备" in account["security_risk_reason"]
    assert {"外部设备未清理", "二步验证待处理", "资料待初始化"}.issubset({item["item_type"] for item in summary["disposition_queue"]})
    assert preflight["decision"] in {"warn", "block"}
    assert preflight["limited_accounts"] or preflight["blocked_accounts"]


def test_disabled_proxy_creates_alert_and_blocks_preflight():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgAccount(id=11, tenant_id=1, display_name="代理账号", phone_masked="11", status=AccountStatus.ACTIVE.value, health_score=90, proxy_id=31))
        session.add(AccountProxy(id=31, tenant_id=1, name="local-1080", host="127.0.0.1", port=1080, status="healthy", alert_status="normal"))
        session.commit()

        disable_account_proxy(session, 1, 31, "代理端口维护", "tester")
        summary = risk_control_summary(session, 1)
        preflight = risk_preflight(session, 1, RiskPreflightRequest(account_ids=[11], proxy_ids=[31], content_preview="正常消息"))

    account = summary["account_scores"][0]
    assert account["risk_level"] == "E"
    assert account["can_join_task"] is False
    assert account["proxy_risk_reason"] == "代理已禁用"
    assert summary["proxy_alerts"][0]["reason_code"] == "proxy_disabled"
    assert any(item["item_type"] == "代理异常" for item in summary["disposition_queue"])
    assert preflight["decision"] == "block"
    assert "proxy_disabled" in preflight["decision_reasons"]


def test_message_send_task_is_blocked_by_risk_preflight_when_proxy_disabled():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgAccount(id=11, tenant_id=1, display_name="代理账号", phone_masked="11", status=AccountStatus.ACTIVE.value, health_score=90, proxy_id=31))
        session.add(AccountProxy(id=31, tenant_id=1, name="local-1080", host="127.0.0.1", port=1080, status="disabled", alert_status="disabled"))
        session.commit()

        with pytest.raises(ValueError, match="风控预检未通过"):
            create_message_send_task(
                session,
                MessageSendTaskCreate(account_id=11, target_type="private", target_peer_id="@target", target_display="target", content="正常消息"),
                "tester",
                tenant_id=1,
            )


def test_preflight_blocks_missing_or_empty_account_scope():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.commit()

        empty = risk_preflight(session, 1, RiskPreflightRequest(content_preview="正常消息"))
        missing = risk_preflight(session, 1, RiskPreflightRequest(account_ids=[999], content_preview="正常消息"))

    assert empty["decision"] == "block"
    assert "no_available_account" in empty["decision_reasons"]
    assert missing["decision"] == "block"
    assert "account_missing" in missing["decision_reasons"]
    assert missing["blocked_accounts"][0]["account_id"] == 999


def test_proxy_binding_precheck_rejects_disabled_proxy():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgAccount(id=11, tenant_id=1, display_name="代理账号", phone_masked="11", status=AccountStatus.ACTIVE.value, health_score=90))
        session.add(AccountProxy(id=31, tenant_id=1, name="local-1080", host="127.0.0.1", port=1080, status="disabled", alert_status="disabled"))
        session.commit()

        with pytest.raises(ValueError, match="代理预检查未通过"):
            bind_account_proxy(session, 1, 11, ProxyBindingRequest(proxy_id=31, change_reason="测试绑定"), "tester")
        forced = bind_account_proxy(session, 1, 11, ProxyBindingRequest(proxy_id=31, change_reason="强制绑定", run_precheck=False), "tester")

    assert forced["new_proxy_id"] == 31


def test_manual_proxy_alert_resolve_requires_healthy_proxy_state():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(AccountProxy(id=31, tenant_id=1, name="local-1080", host="127.0.0.1", port=1080, status="unhealthy", alert_status="alerting", last_error="connect refused"))
        session.add(ProxyAlert(id=41, tenant_id=1, proxy_id=31, status="alerting", severity="critical", reason_code="proxy_unreachable"))
        session.commit()

        with pytest.raises(ValueError, match="健康检查"):
            update_proxy_alert_status(session, 1, 41, "recovered", "tester", reason="人工恢复")

        assert session.get(ProxyAlert, 41).status == "alerting"
        session.commit()
        assert session.get(ProxyAlert, 41).status == "alerting"

        proxy = session.get(AccountProxy, 31)
        proxy.status = "healthy"
        proxy.alert_status = "normal"
        session.commit()
        recovered = update_proxy_alert_status(session, 1, 41, "recovered", "tester", reason="检查后恢复")

    assert recovered["alert_status"] == "recovered"


def test_proxy_alert_ignore_requires_reason_and_expiry():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    ignored_until = _now()

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(AccountProxy(id=31, tenant_id=1, name="local-1080", host="127.0.0.1", port=1080, status="unhealthy", alert_status="alerting"))
        session.add(ProxyAlert(id=41, tenant_id=1, proxy_id=31, severity="critical", status="alerting", reason_code="proxy_timeout"))
        session.commit()

        with pytest.raises(ValueError, match="忽略代理告警必须填写原因"):
            update_proxy_alert_status(session, 1, 41, "ignored", "tester", reason="", ignored_until=ignored_until)
        with pytest.raises(ValueError, match="忽略代理告警必须设置过期时间"):
            update_proxy_alert_status(session, 1, 41, "ignored", "tester", reason="短时波动", ignored_until=None)

        ignored = update_proxy_alert_status(session, 1, 41, "ignored", "tester", reason="短时波动", ignored_until=ignored_until)

    assert ignored["alert_status"] == "ignored"


def test_successful_proxy_check_recovers_existing_alert():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with listening_port() as port:
        with Session(engine) as session:
            session.add(Tenant(id=1, name="默认运营空间"))
            session.add(TgAccount(id=11, tenant_id=1, display_name="代理账号", phone_masked="11", status=AccountStatus.ACTIVE.value, health_score=90, proxy_id=31))
            session.add(AccountProxy(id=31, tenant_id=1, name="local-live", host="127.0.0.1", port=port, status="unhealthy", alert_status="alerting", last_error="connect refused"))
            session.add(ProxyAlert(id=41, tenant_id=1, proxy_id=31, status="alerting", severity="critical", reason_code="proxy_unreachable"))
            session.commit()

            check = check_account_proxy(session, 1, 31, check_type="quick", reason="测试恢复", actor="tester")
            summary = risk_control_summary(session, 1)
            proxy = session.get(AccountProxy, 31)
            alert = session.get(ProxyAlert, 41)

    assert check["status"] == "healthy"
    assert proxy.status == "healthy"
    assert proxy.alert_status == "normal"
    assert alert.status == "recovered"
    assert not any(item["key"] == "proxy-alert:41" for item in summary["disposition_queue"])

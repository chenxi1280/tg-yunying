from contextlib import contextmanager
import socket

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import AccountProxy, AccountStatus, Action, FailureType, MessageTask, ProxyAlert, SchedulingSetting, Task, TaskStatus, Tenant, TgAccount
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

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    AccountEnvironmentBinding,
    AccountProxy,
    AccountProxyBinding,
    AccountProxyWarmupState,
    AccountStatus,
    Action,
    ProxyAirportNode,
    ProxyAirportSubscription,
    ProxyNodeFailoverEvent,
    SearchJoinLinkedTaskDispatch,
    Task,
    TelegramDeveloperApp,
    Tenant,
    TgAccount,
    TgAccountAuthorization,
)
from app.security import encrypt_secret
from app.integrations.telegram import SendResult
from app.services._common import _now
from app.services.task_center import dispatcher
from app.services.task_center.dispatcher import dispatch_action
from app.services.task_center.search_join_linking import create_linked_dispatch_if_membership_observed


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Tenant(id=1, name="默认运营空间"))
        db.add(TelegramDeveloperApp(id=1, app_name="测试应用", api_id=12345, api_hash_ciphertext=encrypt_secret("hash")))
        db.add(AccountProxy(id=31, tenant_id=1, name="节点A", protocol="socks5", host="127.0.0.1", port=1080, status="healthy", alert_status="normal"))
        db.add(AccountProxyBinding(id=301, tenant_id=1, account_id=101, developer_app_id=1, developer_app_api_id_snapshot=12345, authorization_id=201, session_role="primary", proxy_id=31))
        db.add(
            TgAccount(
                id=101,
                tenant_id=1,
                display_name="账号1",
                phone_masked="101",
                status=AccountStatus.ACTIVE.value,
                session_ciphertext="s1",
                developer_app_id=1,
                developer_app_version=1,
            )
        )
        db.add(
            TgAccountAuthorization(
                id=201,
                tenant_id=1,
                account_id=101,
                role="primary",
                developer_app_id=1,
                developer_app_api_id_snapshot=12345,
                proxy_id=31,
                session_ciphertext="slot-session-201",
                status="active",
                health_status="healthy",
                is_current=True,
            )
        )
        db.add(
            AccountEnvironmentBinding(
                id="env-201",
                tenant_id=1,
                account_id=101,
                developer_app_id=1,
                developer_app_api_id_snapshot=12345,
                authorization_id=201,
                session_role="primary",
                proxy_binding_id=301,
                proxy_id=31,
                device_model="iPhone 15",
                system_version="iOS 17.5",
                app_version="10.14.1",
                platform="ios",
                client_identity_key="identity-201",
            )
        )
        db.commit()
        yield db


def _task() -> Task:
    return Task(tenant_id=1, name="搜索入群", type="search_join_group", status="running", type_config={}, stats={})


def _action(task: Task, result: dict | None = None) -> Action:
    return Action(
        tenant_id=1,
        task_id=task.id,
        task_type="search_join_group",
        action_type="search_join",
        account_id=101,
        status="pending",
        payload={
            "execution_mode": "mtproto_userbot",
            "bot_username": "jisou",
            "keyword_hash": "a" * 64,
            "keyword_text_ciphertext": encrypt_secret("上海 留学"),
            "authorization_id": 201,
            "session_role": "primary",
            "client_metadata": {
                "device_model": "iPhone 15",
                "system_version": "iOS 17.5",
                "app_version": "10.14.1",
                "platform": "ios",
                "client_identity_key": "identity-201",
            },
            "target_operation_target_id": 17,
            "target_username": "shanghai",
            "safe_navigation": {"total_max": 3, "decoy_join_enabled": False},
            "search_visibility_attribution": {},
            "post_join_policy": "stay_joined",
            "hourly_execution": {},
            "linked_task_policy": [],
            "runtime_environment": {},
        },
        result=result or {},
    )


def _persist_task_and_action(session: Session, result: dict | None = None) -> tuple[Task, Action]:
    task = _task()
    session.add(task)
    session.flush()
    action = _action(task, result)
    session.add(action)
    session.commit()
    return task, action


def _verified_runtime() -> dict[str, str]:
    return {
        "proxy_egress_guard": "verified",
        "client_metadata_guard": "verified",
        "developer_app_id": "1",
        "developer_app_api_id": "12345",
        "proxy_id": "31",
        "proxy_binding_id": "301",
        "environment_binding_id": "env-201",
        "client_identity_key": "identity-201",
    }


@pytest.mark.no_postgres
def test_search_join_dispatch_fails_closed_without_gateway_support(session: Session) -> None:
    _task, action = _persist_task_and_action(session)

    assert dispatch_action(session, action) is True
    assert action.status == "skipped"
    assert action.result["error_code"] == "search_join_gateway_unavailable"
    assert action.result["validation_stage"] == "search_join_gateway"


@pytest.mark.no_postgres
def test_search_join_dispatch_calls_gateway_with_session_credentials_and_keyword(monkeypatch, session: Session) -> None:
    _task, action = _persist_task_and_action(session)
    action.payload = {**action.payload, "runtime_environment": _verified_runtime()}
    calls: list[dict] = []

    def execute_search_join(account_id, payload, session_ciphertext, credentials, keyword_text):
        calls.append(
            {
                "account_id": account_id,
                "payload": payload,
                "session_ciphertext": session_ciphertext,
                "api_id": credentials.api_id,
                "proxy_id": credentials.proxy_id,
                "keyword_text": keyword_text,
            }
        )
        return {"success": True, "join_status": "membership_observed", "target_group_id": 17}

    monkeypatch.setattr(dispatcher.gateway, "execute_search_join", execute_search_join, raising=False)

    assert dispatch_action(session, action) is True
    assert action.status == "success"
    assert action.result["join_status"] == "membership_observed"
    assert calls[0]["account_id"] == 101
    assert calls[0]["payload"]["keyword_hash"] == "a" * 64
    assert calls[0]["payload"]["bot_username"] == "jisou"
    assert calls[0]["session_ciphertext"] == "slot-session-201"
    assert calls[0]["api_id"] == 12345
    assert calls[0]["proxy_id"] == 31
    assert calls[0]["keyword_text"] == "上海 留学"


@pytest.mark.no_postgres
def test_search_join_dispatch_uses_environment_proxy_not_authorization_proxy(monkeypatch, session: Session) -> None:
    _task, action = _persist_task_and_action(session)
    session.add(AccountProxy(id=99, tenant_id=1, name="节点B", protocol="socks5", host="127.0.0.2", port=1099, status="healthy", alert_status="normal"))
    authorization = session.get(TgAccountAuthorization, 201)
    authorization.proxy_id = 99
    action.payload = {**action.payload, "runtime_environment": _verified_runtime()}
    calls: list[int | None] = []

    def execute_search_join(_account_id, _payload, _session_ciphertext, credentials, _keyword_text):
        calls.append(credentials.proxy_id)
        return {"success": True, "join_status": "membership_observed", "target_group_id": 17}

    monkeypatch.setattr(dispatcher.gateway, "execute_search_join", execute_search_join, raising=False)

    assert dispatch_action(session, action) is True
    assert calls == [31]


@pytest.mark.no_postgres
def test_group_ai_dispatch_uses_direct_credentials_when_account_has_proxy(monkeypatch, session: Session) -> None:
    account = session.get(TgAccount, 101)
    account.proxy_id = 31
    task = Task(id="task-ai-direct", tenant_id=1, name="活群", type="group_ai_chat", status="running", type_config={}, stats={})
    action = Action(
        id="action-ai-direct",
        tenant_id=1,
        task_id=task.id,
        task_type="group_ai_chat",
        action_type="send_message",
        account_id=101,
        status="pending",
        payload={"chat_id": "-1001", "message_text": "hello"},
    )
    session.add_all([task, action])
    session.commit()
    calls: list[int | None] = []

    def send_message_to_target(_account_id, _target_peer, _content, _target_type, _target_pk, _session_ciphertext, credentials):
        calls.append(credentials.proxy_id)
        return SendResult(ok=True, remote_message_id="123")

    monkeypatch.setattr(dispatcher.gateway, "send_message_to_target", send_message_to_target)

    assert dispatch_action(session, action) is True
    assert action.status == "success"
    assert calls == [None]


@pytest.mark.no_postgres
def test_search_join_dispatch_blocks_inactive_environment_proxy_binding(monkeypatch, session: Session) -> None:
    _task, action = _persist_task_and_action(session)
    proxy_binding = session.get(AccountProxyBinding, 301)
    proxy_binding.status = "inactive"
    action.payload = {**action.payload, "runtime_environment": _verified_runtime()}
    calls: list[dict] = []

    def execute_search_join(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        return {"success": True}

    monkeypatch.setattr(dispatcher.gateway, "execute_search_join", execute_search_join, raising=False)

    assert dispatch_action(session, action) is True
    assert calls == []
    assert action.status == "failed"
    assert action.result["error_code"] == "search_join_proxy_binding_environment_mismatch"


@pytest.mark.no_postgres
def test_search_join_dispatch_blocks_gateway_without_proxy_guard(monkeypatch, session: Session) -> None:
    _task, action = _persist_task_and_action(session)
    calls: list[dict] = []

    def execute_search_join(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        return {"success": True}

    monkeypatch.setattr(dispatcher.gateway, "execute_search_join", execute_search_join, raising=False)

    assert dispatch_action(session, action) is True
    assert calls == []
    assert action.status == "failed"
    assert action.result["error_code"] == "proxy_egress_guard_missing"


@pytest.mark.no_postgres
def test_search_join_dispatch_blocks_incomplete_environment_proxy_config(monkeypatch, session: Session) -> None:
    _task, action = _persist_task_and_action(session)
    action.payload = {**action.payload, "runtime_environment": _verified_runtime()}
    proxy = session.get(AccountProxy, 31)
    proxy.host = ""
    calls: list[dict] = []

    def execute_search_join(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        return {"success": True}

    monkeypatch.setattr(dispatcher.gateway, "execute_search_join", execute_search_join, raising=False)

    assert dispatch_action(session, action) is True
    assert calls == []
    assert action.status == "failed"
    assert action.result["error_code"] == "search_join_environment_proxy_config_missing"


@pytest.mark.no_postgres
def test_search_join_dispatch_records_proxy_failover_after_gateway_proxy_failure(monkeypatch, session: Session) -> None:
    task, action = _persist_task_and_action(session)
    action.payload = {**action.payload, "runtime_environment": _verified_runtime()}
    pending = _action(task)
    pending.payload = {**pending.payload, "runtime_environment": _verified_runtime()}
    session.add(pending)
    session.flush()
    primary = ProxyAirportSubscription(
        tenant_id=1,
        name="primary",
        subscription_url_ciphertext="enc",
        priority=10,
        enabled=True,
        sync_status="synced",
        node_count=2,
        healthy_node_count=1,
    )
    session.add(primary)
    session.flush()
    old_node = ProxyAirportNode(
        tenant_id=1,
        subscription_id=primary.id,
        node_key="old-node",
        node_name="old-node",
        protocol="trojan",
        proxy_host="old.example.com",
        proxy_port=443,
        status="unhealthy",
        observed_exit_ip="203.0.113.10",
    )
    next_node = ProxyAirportNode(
        tenant_id=1,
        subscription_id=primary.id,
        node_key="next-node",
        node_name="next-node",
        protocol="trojan",
        proxy_host="next.example.com",
        proxy_port=443,
        status="healthy",
        observed_exit_ip="203.0.113.11",
    )
    session.add_all([old_node, next_node])
    session.flush()
    session.get(AccountProxyBinding, 301).proxy_airport_node_id = old_node.id

    def execute_search_join(*args, **kwargs):
        return {
            "success": False,
            "error_code": "proxy_node_unreachable",
            "error_message": "connect_timeout",
        }

    monkeypatch.setattr(dispatcher.gateway, "execute_search_join", execute_search_join, raising=False)

    assert dispatch_action(session, action) is True
    old_binding = session.get(AccountProxyBinding, 301)
    new_binding = session.query(AccountProxyBinding).filter(AccountProxyBinding.id != 301).one()
    environment = session.get(AccountEnvironmentBinding, "env-201")
    event = session.query(ProxyNodeFailoverEvent).one()
    warmup = session.query(AccountProxyWarmupState).one()

    assert action.status == "failed"
    assert action.result["proxy_failover_status"] == "switched"
    assert action.result["proxy_failover_event_id"] == event.id
    assert old_binding.status == "inactive"
    assert new_binding.proxy_airport_node_id == next_node.id
    assert environment.proxy_binding_id == new_binding.id
    assert environment.proxy_id == new_binding.proxy_id
    assert pending.payload["runtime_environment"]["proxy_binding_id"] == str(new_binding.id)
    assert pending.payload["runtime_environment"]["proxy_id"] == str(new_binding.proxy_id)
    assert new_binding.binding_generation == 2
    assert event.reason == "proxy_node_unreachable"
    assert event.observed_error == "connect_timeout"
    assert warmup.proxy_binding_id == new_binding.id
    assert warmup.stage == "pending_warmup"


@pytest.mark.no_postgres
def test_search_join_dispatch_records_admin_notice_when_runtime_failover_has_no_candidate(monkeypatch, session: Session) -> None:
    _task, action = _persist_task_and_action(session)
    action.payload = {**action.payload, "runtime_environment": _verified_runtime()}
    primary = ProxyAirportSubscription(
        tenant_id=1,
        name="primary",
        subscription_url_ciphertext="enc",
        priority=10,
        enabled=True,
        sync_status="synced",
        node_count=1,
        healthy_node_count=0,
    )
    session.add(primary)
    session.flush()
    old_node = ProxyAirportNode(
        tenant_id=1,
        subscription_id=primary.id,
        node_key="old-node",
        node_name="old-node",
        protocol="trojan",
        proxy_host="old.example.com",
        proxy_port=443,
        status="unhealthy",
        observed_exit_ip="203.0.113.10",
    )
    session.add(old_node)
    session.flush()
    session.get(AccountProxyBinding, 301).proxy_airport_node_id = old_node.id

    def execute_search_join(*args, **kwargs):
        return {
            "success": False,
            "error_code": "proxy_node_unreachable",
            "error_message": "connect_timeout",
        }

    monkeypatch.setattr(dispatcher.gateway, "execute_search_join", execute_search_join, raising=False)

    assert dispatch_action(session, action) is True
    assert action.status == "failed"
    assert action.result["proxy_failover_status"] == "failed"
    assert action.result["proxy_failover_error"] == "airport_all_subscriptions_unavailable"
    assert action.result["admin_notification_status"] == "admin_notification_failed"
    assert "Telegram Bot token" in action.result["admin_notification_detail"]


@pytest.mark.no_postgres
def test_search_join_dispatch_creates_linked_records_after_membership_observed(monkeypatch, session: Session) -> None:
    _task, action = _persist_task_and_action(session)
    action.payload = {
        **action.payload,
        "runtime_environment": _verified_runtime(),
        "linked_task_policy": [{"linked_task_id": "ai-task-1", "cooldown_minutes": 30}],
    }

    def execute_search_join(*args, **kwargs):
        return {"success": True, "join_status": "membership_observed", "target_group_id": 17}

    monkeypatch.setattr(dispatcher.gateway, "execute_search_join", execute_search_join, raising=False)

    assert dispatch_action(session, action) is True
    dispatch = session.scalar(select(SearchJoinLinkedTaskDispatch).where(SearchJoinLinkedTaskDispatch.search_join_action_id == action.id))
    assert dispatch is not None
    assert dispatch.linked_task_id == "ai-task-1"
    assert dispatch.block_reason == "cooldown_waiting"


@pytest.mark.no_postgres
def test_search_join_dispatch_stops_task_when_target_not_found_after_max_pages(monkeypatch, session: Session) -> None:
    task, action = _persist_task_and_action(session)
    action.payload = {
        **action.payload,
        "max_pages": 70,
        "runtime_environment": _verified_runtime(),
    }
    pending = _action(task)
    pending.payload = dict(action.payload)
    session.add(pending)
    session.commit()

    def execute_search_join(*args, **kwargs):
        return {
            "success": False,
            "error_code": "target_not_in_results",
            "detail": "目标群未出现在搜索结果",
            "join_status": "failed",
            "page": 70,
            "max_pages": 70,
            "pages_exhausted": True,
        }

    monkeypatch.setattr(dispatcher.gateway, "execute_search_join", execute_search_join, raising=False)

    assert dispatch_action(session, action) is True
    assert action.status == "failed"
    assert task.status == "stopped"
    assert "70" in task.last_error
    assert pending.status == "skipped"
    assert pending.result["error_code"] == "search_join_target_not_found_task_stopped"


@pytest.mark.no_postgres
def test_linked_dispatch_requires_membership_observed(session: Session) -> None:
    _task, failed = _persist_task_and_action(session, {"join_status": "target_not_observed"})

    assert create_linked_dispatch_if_membership_observed(session, failed, linked_task_id="ai-task-1") is None
    assert session.scalar(select(SearchJoinLinkedTaskDispatch)) is None


@pytest.mark.no_postgres
def test_linked_dispatch_records_success_cooldown_and_can_send(session: Session) -> None:
    _task, action = _persist_task_and_action(session, {"join_status": "membership_observed", "target_group_id": 17})
    activation = _now() + timedelta(minutes=30)

    dispatch = create_linked_dispatch_if_membership_observed(
        session,
        action,
        linked_task_id="ai-task-1",
        activation_not_before=activation,
        can_send_checked_at=_now(),
    )

    assert dispatch is not None
    assert dispatch.status == "linked_task_ready_pending"
    assert dispatch.link_type == "group_ai_chat"
    assert dispatch.activation_not_before == activation

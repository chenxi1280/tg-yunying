from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import AccountStatus, Action, SearchJoinLinkedTaskDispatch, Task, TelegramDeveloperApp, Tenant, TgAccount
from app.security import encrypt_secret
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
    action.payload = {**action.payload, "runtime_environment": {"proxy_egress_guard": "verified", "client_metadata_guard": "verified"}}
    calls: list[dict] = []

    def execute_search_join(account_id, payload, session_ciphertext, credentials, keyword_text):
        calls.append(
            {
                "account_id": account_id,
                "payload": payload,
                "session_ciphertext": session_ciphertext,
                "api_id": credentials.api_id,
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
    assert calls[0]["session_ciphertext"] == "s1"
    assert calls[0]["api_id"] == 12345
    assert calls[0]["keyword_text"] == "上海 留学"


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
def test_search_join_dispatch_creates_linked_records_after_membership_observed(monkeypatch, session: Session) -> None:
    _task, action = _persist_task_and_action(session)
    action.payload = {
        **action.payload,
        "runtime_environment": {"proxy_egress_guard": "verified", "client_metadata_guard": "verified"},
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
        "runtime_environment": {"proxy_egress_guard": "verified", "client_metadata_guard": "verified"},
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

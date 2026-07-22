from __future__ import annotations

import hashlib
from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.integrations.telegram import DeveloperAppCredentials, OperationResult
from app.models import (
    AccountEnvironmentBinding,
    AccountProxy,
    AccountProxyBinding,
    AccountStatus,
    Action,
    SearchJoinLinkedTaskDispatch,
    Task,
    TelegramDeveloperApp,
    Tenant,
    TgAccount,
    TgAccountAuthorization,
)
from app.security import encrypt_secret
from app.services.task_center import dispatcher
from app.services.task_center.dispatcher import dispatch_action
from app.services.task_center.payloads import SearchJoinMembershipPayload, SearchJoinPayload
from app.schemas.task_center import TaskRetryRequest
from app.services.task_center.service import retry_task
from app.services.task_center.search_click_target_progress import (
    reconcile_search_click_target_progress,
    search_click_target_progress,
    search_join_membership_target_progress,
)
from app.services.task_center.search_join_pacing import PacingStats, PacingWindow, account_base_allowed, keyword_allowed


KEYWORD_HASH = hashlib.sha256("郑州".encode("utf-8")).hexdigest()


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Tenant(id=1, name="默认运营空间"))
        db.add(TelegramDeveloperApp(id=1, app_name="测试应用", api_id=12345, api_hash_ciphertext=encrypt_secret("hash")))
        db.add(AccountProxy(id=31, tenant_id=1, name="节点A", protocol="socks5", host="127.0.0.1", port=1080, status="healthy", alert_status="normal"))
        db.add(AccountProxyBinding(id=301, tenant_id=1, account_id=101, developer_app_id=1, developer_app_api_id_snapshot=12345, authorization_id=201, session_role="primary", proxy_id=31))
        db.add(TgAccount(id=101, tenant_id=1, display_name="账号1", username="source-account", phone_masked="101", status=AccountStatus.ACTIVE.value, session_ciphertext="primary-account-session", developer_app_id=1, developer_app_version=1))
        db.add(TgAccountAuthorization(id=201, tenant_id=1, account_id=101, role="primary", developer_app_id=1, developer_app_api_id_snapshot=12345, proxy_id=31, session_ciphertext="slot-session-201", status="active", health_status="healthy", is_current=True))
        db.add(AccountEnvironmentBinding(id="env-201", tenant_id=1, account_id=101, developer_app_id=1, developer_app_api_id_snapshot=12345, authorization_id=201, session_role="primary", proxy_binding_id=301, proxy_id=31, device_model="iPhone 15", system_version="iOS 17.5", app_version="10.14.1", platform="ios", client_identity_key="identity-201"))
        db.commit()
        yield db


def _runtime() -> dict[str, str]:
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


def _source_payload() -> dict:
    return {
        "execution_mode": "mtproto_userbot",
        "bot_username": "jisou",
        "keyword_hash": KEYWORD_HASH,
        "keyword_text_ciphertext": encrypt_secret("郑州"),
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
        "target_group_id": 17,
        "target_username": "zzxshxc",
        "target_title": "河南郑州学生会",
        "target_peer_id": "-1003298633687",
        "safe_navigation": {"total_max": 1, "decoy_join_enabled": False},
        "search_visibility_attribution": {},
        "post_join_policy": "stay_joined",
        "hourly_execution": {},
        "linked_task_policy": [{"linked_task_id": "ai-task-1"}],
        "runtime_environment": _runtime(),
    }


def _source_action(session: Session) -> tuple[Task, Action]:
    task = Task(tenant_id=1, name="郑州搜索", type="search_join_group", status="running", type_config={"daily_target_count": 1}, stats={})
    session.add(task)
    session.flush()
    action = Action(tenant_id=1, task_id=task.id, task_type=task.type, action_type="search_join", account_id=101, status="pending", payload=_source_payload(), result={})
    session.add(action)
    session.commit()
    return task, action


def _target_found_result() -> dict:
    return {"success": True, "join_status": "target_found", "search_end_reason": "target_found", "target_group_id": 17}


@pytest.mark.no_postgres
def test_target_found_creates_one_scoped_membership_child(monkeypatch, session: Session) -> None:
    _task, source = _source_action(session)
    monkeypatch.setattr(dispatcher.gateway, "execute_search_join", lambda *_args: _target_found_result(), raising=False)

    assert dispatch_action(session, source) is True

    children = list(session.scalars(select(Action).where(Action.task_id == source.task_id, Action.action_type == "search_join_membership")))
    assert source.status == "success"
    assert source.result["join_status"] == "membership_pending"
    assert source.result["target_click_observed"] is True
    assert source.result["target_found_at"]
    assert len(children) == 1
    assert children[0].payload["source_search_join_action_id"] == source.id
    assert SearchJoinMembershipPayload.model_validate(children[0].payload).target_username == "zzxshxc"


@pytest.mark.no_postgres
def test_membership_child_uses_source_slot_and_only_then_counts(monkeypatch, session: Session) -> None:
    _task, source = _source_action(session)
    monkeypatch.setattr(dispatcher.gateway, "execute_search_join", lambda *_args: _target_found_result(), raising=False)
    assert dispatch_action(session, source) is True
    child = session.scalar(select(Action).where(Action.task_id == source.task_id, Action.action_type == "search_join_membership"))
    assert child is not None
    calls: list[dict] = []

    def ensure(account_id, payload, session_ciphertext, credentials):
        calls.append({"account_id": account_id, "payload": payload, "session": session_ciphertext, "credentials": credentials})
        return {"success": True, "join_status": "membership_observed", "target_group_id": 17}

    monkeypatch.setattr(dispatcher.gateway, "ensure_search_join_membership", ensure, raising=False)
    monkeypatch.setattr(dispatcher.gateway, "ensure_channel_membership", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not use primary session admission")), raising=False)

    assert dispatch_action(session, child) is True

    assert child.status == "success"
    assert source.result["join_status"] == "membership_observed"
    assert source.result["membership_observed_at"]
    assert calls[0]["account_id"] == 101
    assert calls[0]["session"] == "slot-session-201"
    assert isinstance(calls[0]["credentials"], DeveloperAppCredentials)
    assert calls[0]["credentials"].proxy_id == 31
    assert session.scalar(select(SearchJoinLinkedTaskDispatch).where(SearchJoinLinkedTaskDispatch.search_join_action_id == source.id)) is not None


@pytest.mark.no_postgres
def test_retry_rebinds_search_membership_child_to_source_account(session: Session) -> None:
    task, source = _source_action(session)
    task.type_config = {"daily_click_target_count": 500, "daily_target_count": 80}
    source.result = {"success": True, "join_status": "membership_pending", "target_click_observed": True}
    session.add(
        TgAccount(
            id=102,
            tenant_id=1,
            display_name="误转派账号",
            username="reassigned-account",
            phone_masked="102",
            status=AccountStatus.ACTIVE.value,
            session_ciphertext="reassigned-session",
            developer_app_id=1,
            developer_app_version=1,
        )
    )
    child = dispatcher.create_membership_child(
        session,
        source,
        SearchJoinPayload.model_validate(_source_payload()),
        datetime(2026, 7, 22, 19, 56),
    )
    child.status = "failed"
    child.account_id = 102
    session.commit()

    assert dispatcher._action_can_reassign(child) is False

    retry_task(session, 1, task.id, TaskRetryRequest(failed_only=True), "tester")

    assert child.status == "pending"
    assert child.account_id == source.account_id


@pytest.mark.no_postgres
def test_dispatch_rebinds_legacy_membership_child_to_source_account(monkeypatch, session: Session) -> None:
    _task, source = _source_action(session)
    monkeypatch.setattr(dispatcher.gateway, "execute_search_join", lambda *_args: _target_found_result(), raising=False)
    assert dispatch_action(session, source) is True
    child = session.scalar(select(Action).where(Action.task_id == source.task_id, Action.action_type == "search_join_membership"))
    assert child is not None
    session.add(TgAccount(
        id=102,
        tenant_id=1,
        display_name="误转派账号",
        username="reassigned-account",
        phone_masked="102",
        status=AccountStatus.ACTIVE.value,
        session_ciphertext="reassigned-session",
        developer_app_id=1,
        developer_app_version=1,
    ))
    child.account_id = 102
    session.commit()
    calls: list[int] = []
    monkeypatch.setattr(
        dispatcher.gateway,
        "ensure_search_join_membership",
        lambda account_id, *_args: calls.append(account_id) or {"success": True, "join_status": "membership_observed", "target_group_id": 17},
        raising=False,
    )

    assert dispatch_action(session, child) is True

    assert child.account_id == source.account_id
    assert child.status == "success"
    assert calls == [source.account_id]


@pytest.mark.no_postgres
def test_dispatch_rebinds_legacy_search_source_to_authorization_account(monkeypatch, session: Session) -> None:
    _task, source = _source_action(session)
    session.add(TgAccount(
        id=102,
        tenant_id=1,
        display_name="误转派账号",
        username="reassigned-account",
        phone_masked="102",
        status=AccountStatus.ACTIVE.value,
        session_ciphertext="reassigned-session",
        developer_app_id=1,
        developer_app_version=1,
    ))
    source.account_id = 102
    session.commit()
    calls: list[int] = []
    monkeypatch.setattr(
        dispatcher.gateway,
        "execute_search_join",
        lambda account_id, *_args: calls.append(account_id) or _target_found_result(),
        raising=False,
    )

    assert dispatcher._action_can_reassign(source) is False
    assert dispatch_action(session, source) is True

    assert source.account_id == 101
    assert source.status == "success"
    assert calls == [101]


@pytest.mark.no_postgres
def test_claim_rebinds_source_before_account_policy_and_does_not_reassign(monkeypatch, session: Session) -> None:
    _task, source = _source_action(session)
    session.add(TgAccount(
        id=102,
        tenant_id=1,
        display_name="错误转派账号",
        username="wrong-claim-account",
        phone_masked="102",
        status=AccountStatus.ACTIVE.value,
        session_ciphertext="wrong-session",
        developer_app_id=1,
        developer_app_version=1,
    ))
    source.account_id = 102
    session.commit()
    checked_accounts: list[int] = []

    def unavailable(*_args, account_id: int, **_kwargs):
        checked_accounts.append(account_id)
        return SimpleNamespace(
            available=False,
            defer_until=datetime(2026, 7, 23, 10, 0),
            reason="账号全局冷却中",
        )

    monkeypatch.setattr(dispatcher, "account_capacity_decision", unavailable)
    monkeypatch.setattr(dispatcher, "_replacement_account_for_action", lambda *_args: session.get(TgAccount, 101))

    assert dispatcher.claim_actions(session, limit=1, worker_id="worker-test") == []

    action = session.get(Action, source.id)
    assert checked_accounts == [101]
    assert action is not None
    assert action.account_id == 101
    assert action.status == "pending"
    assert action.result["error_code"] == "global_account_policy"


@pytest.mark.no_postgres
def test_pending_application_uses_rescue_admin_then_reprobes_source_slot(monkeypatch, session: Session) -> None:
    tenant = session.get(Tenant, 1)
    assert tenant is not None
    tenant.group_rescue_admin_account_id = 102
    session.add(TgAccount(id=102, tenant_id=1, display_name="救援管理员", username="rescue-admin", phone_masked="102", status=AccountStatus.ACTIVE.value, session_ciphertext="admin-session-102", developer_app_id=1, developer_app_version=1))
    session.commit()
    _task, source = _source_action(session)
    monkeypatch.setattr(dispatcher.gateway, "execute_search_join", lambda *_args: _target_found_result(), raising=False)
    assert dispatch_action(session, source) is True
    child = session.scalar(select(Action).where(Action.task_id == source.task_id, Action.action_type == "search_join_membership"))
    assert child is not None
    calls: list[tuple[str, int, str, str]] = []

    monkeypatch.setattr(
        dispatcher.gateway,
        "ensure_search_join_membership",
        lambda *_args: {"success": False, "error_code": "join_request_pending", "join_status": "join_request_pending", "detail": "已提交入群申请，等待审批"},
        raising=False,
    )
    monkeypatch.setattr(
        dispatcher.gateway,
        "approve_group_join_request",
        lambda admin_id, group_peer_id, target_ref, session_ciphertext, _credentials: calls.append(("approve", admin_id, group_peer_id, target_ref)) or OperationResult(True, "已处理", detail=session_ciphertext),
        raising=False,
    )

    def probe(account_id, _payload, session_ciphertext, _credentials):
        calls.append(("probe", account_id, session_ciphertext, ""))
        return {"success": True, "join_status": "membership_observed", "target_group_id": 17}

    monkeypatch.setattr(dispatcher.gateway, "probe_search_join_membership", probe, raising=False)

    assert dispatch_action(session, child) is True

    assert calls == [
        ("approve", 102, "@zzxshxc", "@source-account"),
        ("probe", 101, "slot-session-201", ""),
    ]
    assert child.status == "success"
    assert child.result["join_request_approval_status"] == "approved"
    assert child.result["join_request_approval_detail"] == "admin-session-102"
    assert source.result["join_status"] == "membership_observed"


@pytest.mark.no_postgres
def test_pending_application_reprobes_without_reapplying_and_blocks_next_day(monkeypatch, session: Session) -> None:
    task, source = _source_action(session)
    monkeypatch.setattr(dispatcher.gateway, "execute_search_join", lambda *_args: _target_found_result(), raising=False)
    assert dispatch_action(session, source) is True
    child = session.scalar(select(Action).where(Action.task_id == task.id, Action.action_type == "search_join_membership"))
    assert child is not None
    calls = {"apply": 0, "probe": 0}

    def apply(*_args):
        calls["apply"] += 1
        return {"success": False, "error_code": "join_request_pending", "join_status": "join_request_pending", "detail": "已提交入群申请，等待审批"}

    monkeypatch.setattr(dispatcher.gateway, "ensure_search_join_membership", apply, raising=False)
    assert dispatch_action(session, child) is True
    assert child.status == "pending"
    assert source.result["join_status"] == "membership_pending"
    assert calls["apply"] == 1
    tomorrow = PacingWindow(local_date=datetime(2026, 7, 23).date(), hour_start=datetime(2026, 7, 23, 10))
    assert account_base_allowed(session, task, 101, tomorrow, PacingStats()) is False

    def probe(*_args):
        calls["probe"] += 1
        return {"success": True, "join_status": "membership_observed", "target_group_id": 17}

    monkeypatch.setattr(dispatcher.gateway, "probe_search_join_membership", probe, raising=False)
    assert dispatch_action(session, child) is True
    assert child.status == "success"
    assert source.result["join_status"] == "membership_observed"
    assert calls == {"apply": 1, "probe": 1}


@pytest.mark.no_postgres
def test_pending_reprobe_clears_stale_membership_observed_fact(monkeypatch, session: Session) -> None:
    task, source = _source_action(session)
    monkeypatch.setattr(dispatcher.gateway, "execute_search_join", lambda *_args: _target_found_result(), raising=False)
    assert dispatch_action(session, source) is True
    child = session.scalar(select(Action).where(Action.task_id == task.id, Action.action_type == "search_join_membership"))
    assert child is not None
    child.result = {
        "application_submitted_at": "2026-07-22T20:50:00+08:00",
        "join_status": "join_request_pending",
        "membership_observed": True,
    }
    session.commit()
    monkeypatch.setattr(
        dispatcher.gateway,
        "probe_search_join_membership",
        lambda *_args: {"success": False, "error_code": "membership_not_observed", "join_status": "membership_pending"},
        raising=False,
    )

    assert dispatch_action(session, child) is True

    assert child.status == "pending"
    assert "membership_observed" not in child.result


@pytest.mark.no_postgres
def test_repeat_application_mode_allows_same_account_after_pending_request(session: Session) -> None:
    task, source = _source_action(session)
    task.type_config = {
        "daily_click_target_count": 500,
        "daily_target_count": 80,
        "allow_same_account_repeat_application": True,
    }
    task.pacing_config = {
        "per_account_daily_action_limit": 1,
        "per_keyword_account_daily_limit": 1,
    }
    source.status = "success"
    source.executed_at = datetime(2026, 7, 23, 9, 0)
    source.result = {"join_status": "membership_pending"}
    session.commit()

    window = PacingWindow(local_date=datetime(2026, 7, 23).date(), hour_start=datetime(2026, 7, 23, 10))

    assert account_base_allowed(session, task, 101, window, PacingStats()) is True
    assert keyword_allowed(session, task, 101, KEYWORD_HASH, window, PacingStats()) is True


@pytest.mark.no_postgres
def test_click_and_membership_progress_are_counted_from_distinct_facts(session: Session) -> None:
    task, source = _source_action(session)
    task.type_config = {
        "daily_click_target_count": 500,
        "daily_target_count": 80,
    }
    source.status = "success"
    source.executed_at = datetime(2026, 7, 23, 9, 0)
    source.result = {
        "success": True,
        "join_status": "membership_pending",
        "target_click_observed": True,
        "target_found_at": "2026-07-23T09:00:00+08:00",
    }
    session.commit()

    click_progress = reconcile_search_click_target_progress(
        session, task, now_value=datetime(2026, 7, 23, 10, 0)
    )
    membership_progress = search_join_membership_target_progress(
        session, task, now_value=datetime(2026, 7, 23, 10, 0)
    )

    assert click_progress.confirmed_count == 1
    assert click_progress.held_count == 0
    assert click_progress.remaining_slot_count == 499
    assert membership_progress is not None
    assert membership_progress.confirmed_count == 0
    assert membership_progress.held_count == 1
    assert membership_progress.remaining_slot_count == 79
    assert task.stats["search_click_target"]["confirmed_count"] == 1
    assert task.stats["search_join_membership_target"]["confirmed_count"] == 0


@pytest.mark.no_postgres
def test_daily_progress_uses_membership_observed_time_not_search_time(session: Session) -> None:
    task, source = _source_action(session)
    source.status = "success"
    source.executed_at = datetime(2026, 7, 22, 23, 50)
    source.result = {"join_status": "membership_observed", "membership_observed_at": "2026-07-23T00:10:00+08:00"}
    session.commit()

    progress = search_click_target_progress(session, task, now_value=datetime(2026, 7, 23, 10, 0))

    assert progress.confirmed_count == 1

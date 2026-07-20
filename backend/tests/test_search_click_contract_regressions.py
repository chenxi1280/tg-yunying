from __future__ import annotations

import hashlib
from datetime import timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.integrations.telegram.search_join import SearchJoinButton, _button_hash, _matches_target
from app.models import (
    AccountPool,
    AccountProxy,
    AccountStatus,
    Action,
    ProxyAirportNode,
    ProxyAirportSubscription,
    Task,
    TelegramDeveloperApp,
    Tenant,
    TgAccount,
    TgGroup,
    OperationTarget,
)
from app.models.search_rank_deboost import AccountGroupProxyBinding, SearchRankDeboostActionStat, SearchRankDeboostClickReservation
from app.security import encrypt_secret, encrypt_session
from app.schemas.task_center import SearchJoinGroupConfig
from app.services._common import _now
from app.services.task_center.executors.search_rank_deboost import execute_search_rank_deboost
from app.services.task_center.payloads import SearchRankDeboostPayload
from app.services.task_center.search_rank_deboost_pacing import DeboostPacingStats, account_click_allowed, deboost_pacing_window
from app.services.task_center.search_rank_deboost_targets import rank_deboost_target_group_refs


pytestmark = pytest.mark.no_postgres


def _engine():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return engine


def _rank_context(session: Session) -> tuple[Task, Action, TgAccount, SearchRankDeboostPayload]:
    now_value = _now()
    session.add_all([
        Tenant(id=1, name="默认运营空间"),
        AccountPool(id=10, tenant_id=1, name="黑搜索分组", pool_purpose="rank_deboost"),
        ProxyAirportSubscription(id=1, tenant_id=1, name="主订阅", enabled=True, sync_status="synced"),
        ProxyAirportNode(id=20, tenant_id=1, subscription_id=1, node_key="node-20", status="healthy", observed_exit_ip="1.1.1.1"),
        AccountProxy(id=30, tenant_id=1, name="分组代理", protocol="socks5", host="127.0.0.1", port=1080, status="healthy", alert_status="normal"),
        TelegramDeveloperApp(id=40, app_name="黑搜索开发者应用", api_id=12345, api_hash_ciphertext=encrypt_secret("api-hash")),
    ])
    account = TgAccount(
        id=100,
        tenant_id=1,
        pool_id=10,
        display_name="黑搜索账号",
        phone_masked="100",
        status=AccountStatus.ACTIVE.value,
        account_identity="rank_deboost",
        developer_app_id=40,
        developer_app_version=1,
        session_ciphertext=encrypt_session("rank-session"),
    )
    task = Task(
        id=str(uuid4()),
        tenant_id=1,
        name="黑搜索任务",
        type="search_rank_deboost",
        status="running",
        timezone="Asia/Shanghai",
        type_config={"per_account_daily_click_limit": 1},
        pacing_config={},
        account_config={},
        failure_policy={},
        stats={},
    )
    binding = AccountGroupProxyBinding(
        id=1,
        tenant_id=1,
        account_pool_id=10,
        proxy_airport_node_id=20,
        runtime_proxy_id=30,
        observed_exit_ip="1.1.1.1",
        binding_generation=1,
        status="active",
    )
    payload = SearchRankDeboostPayload(
        bot_username="jisou",
        keyword_hash="a" * 64,
        keyword_text_ciphertext=encrypt_secret("黑搜索关键词"),
        target_group_ids=[1001],
        account_pool_id=10,
        proxy_airport_node_id=20,
        runtime_environment={
            "group_proxy_binding_id": "1",
            "runtime_proxy_id": "30",
            "binding_generation": "1",
            "account_pool_id": "10",
            "observed_exit_ip": "1.1.1.1",
        },
    )
    action = Action(
        id=str(uuid4()),
        tenant_id=1,
        task_id=task.id,
        task_type=task.type,
        action_type="search_rank_deboost",
        account_id=account.id,
        scheduled_at=now_value,
        status="executing",
        payload=payload.model_dump(mode="json"),
        result={},
    )
    reservation = SearchRankDeboostClickReservation(
        id=str(uuid4()),
        tenant_id=1,
        task_id=task.id,
        action_id=action.id,
        account_id=account.id,
        account_pool_id=10,
        keyword_hash=payload.keyword_hash,
        local_date=now_value.date(),
        hour_bucket=now_value.replace(minute=0, second=0, microsecond=0),
        expires_at=now_value + timedelta(minutes=15),
    )
    session.add_all([account, task, binding, action, reservation])
    session.commit()
    return task, action, account, payload


def test_rank_runtime_passes_authorized_session_and_group_proxy_to_gateway() -> None:
    with Session(_engine()) as session:
        _task, action, account, payload = _rank_context(session)
        captured: dict[str, object] = {}

        def gateway_execute(account_id, gateway_payload, session_ciphertext, credentials, keyword_text):
            captured.update({
                "account_id": account_id,
                "payload": gateway_payload,
                "session_ciphertext": session_ciphertext,
                "credentials": credentials,
                "keyword_text": keyword_text,
            })
            return {
                "success": True,
                "execution_status": "confirmed",
                "observed_exit_ip": "1.1.1.1",
                "click_outcomes": [{
                    "status": "confirmed",
                    "competitor_username": "competitor_a",
                    "competitor_peer_id": "-1002001",
                    "competitor_title": "竞争群 A",
                    "competitor_position": 1,
                    "row": 0,
                    "col": 0,
                    "text": "查看 competitor_a",
                        "url": "https://t.me/competitor_a",
                        "effect": "navigate_only",
                        "joined": False,
                        "dwell_seconds": 7,
                    }],
                }

        result = execute_search_rank_deboost(session, action, account, payload, gateway_execute=gateway_execute)

        assert result["success"] is True
        assert captured["account_id"] == account.id
        assert captured["session_ciphertext"] == account.session_ciphertext
        assert captured["credentials"].proxy_id == 30
        assert captured["keyword_text"] == "黑搜索关键词"
        stat = session.query(SearchRankDeboostActionStat).filter_by(action_id=action.id).one()
        assert stat.competitor_group_username == "competitor_a"
        assert stat.dwell_seconds == 7


def test_rank_daily_limit_applies_to_another_task_in_the_same_tenant() -> None:
    with Session(_engine()) as session:
        first_task, first_action, account, payload = _rank_context(session)
        second_task = Task(
            id=str(uuid4()),
            tenant_id=1,
            name="第二个黑搜索任务",
            type="search_rank_deboost",
            status="running",
            timezone="Asia/Shanghai",
            type_config={"per_account_daily_click_limit": 1},
            pacing_config={},
            account_config={},
            failure_policy={},
            stats={},
        )
        session.add(second_task)
        session.commit()
        window = deboost_pacing_window(second_task, _now())
        allowed = account_click_allowed(
            session,
            second_task,
            account.id,
            payload.keyword_hash,
            10,
            window,
            DeboostPacingStats(),
        )

        assert first_task.id != second_task.id
        assert first_action.id
        assert allowed is False


def test_search_join_rejects_unpaired_keyword_hash_and_ciphertext() -> None:
    with pytest.raises(ValidationError, match="keyword_hashes 与 keyword_text_ciphertexts 必须一一对应"):
        SearchJoinGroupConfig(
            target_group_id=17,
            search_bots=[{"username": "jisou"}],
            keyword_hashes=["a" * 64],
            keyword_text_ciphertexts=[],
        )


def test_search_join_rejects_mismatched_keyword_hash_and_ciphertext() -> None:
    with pytest.raises(ValidationError, match="keyword_hashes 与 keyword_text_ciphertexts 的关键词内容不匹配"):
        SearchJoinGroupConfig(
            target_group_id=17,
            search_bots=[{"username": "jisou"}],
            keyword_hashes=[hashlib.sha256("审计关键词".encode("utf-8")).hexdigest()],
            keyword_text_ciphertexts=[encrypt_secret("实际搜索关键词")],
        )


def test_search_join_target_title_is_not_an_execution_identity() -> None:
    button = SearchJoinButton(
        row=0,
        col=0,
        text="同名目标群",
        button_type="callback_data",
        effect="join_candidate",
        position=1,
        target_username="other_group",
    )

    assert _matches_target(button, {"username": "target_group", "title": "同名目标群", "group_id": 17}) is False


def test_search_join_button_hash_is_stable_sha256() -> None:
    button = SearchJoinButton(
        row=2,
        col=1,
        text="查看",
        button_type="url",
        effect="navigate_only",
        position=3,
        url="https://t.me/example",
    )

    expected = hashlib.sha256("查看:https://t.me/example:3".encode("utf-8")).hexdigest()[:16]
    assert _button_hash(button) == expected


def test_rank_target_reference_collision_is_not_silently_overwritten() -> None:
    with Session(_engine()) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add_all([
            OperationTarget(
                id=17,
                tenant_id=1,
                target_type="group",
                tg_peer_id="-10017",
                title="运营目标",
                username="operation_target",
            ),
            TgGroup(
                id=17,
                tenant_id=1,
                tg_peer_id="-20017",
                title="群记录目标",
            ),
        ])
        session.commit()

        with pytest.raises(ValueError, match="引用类型"):
            rank_deboost_target_group_refs(session, 1, [17])

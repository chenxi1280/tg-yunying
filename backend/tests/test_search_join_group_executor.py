from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    AccountPool,
    AccountEnvironmentBinding,
    AccountProxy,
    AccountProxyBinding,
    AccountStatus,
    Action,
    BotProtocolSample,
    FingerprintComboHistory,
    OperationTarget,
    SearchJoinPacingDecision,
    Task,
    TelegramDeveloperApp,
    Tenant,
    TgAccount,
    TgAccountAuthorization,
)
from app.security import encrypt_secret
from app.search_keywords import normalized_keyword_hash
from app.schemas.account_environment import ProxyAirportSubscriptionCreate
from app.services._common import _now
from app.services.proxy_airport_subscription import create_proxy_airport_subscription, sync_proxy_airport_subscription_by_id
from app.services.task_center.search_join_pacing import pacing_window
from app.services.task_center.executors import build_task_plan
from app.services.task_center.executors import search_join_group as search_join_executor

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Tenant(id=1, name="默认运营空间"))
        db.add(AccountPool(id=1, tenant_id=1, name="普通账号组", pool_purpose="normal", is_enabled=True))
        db.add(OperationTarget(id=17, tenant_id=1, target_type="group", tg_peer_id="-10017", title="上海群", username="shanghai"))
        db.add_all(
            [
                TgAccount(id=101, tenant_id=1, pool_id=1, display_name="账号1", phone_masked="101", status=AccountStatus.ACTIVE.value, account_identity="normal", session_ciphertext="s1"),
                TgAccount(id=102, tenant_id=1, pool_id=1, display_name="账号2", phone_masked="102", status=AccountStatus.ACTIVE.value, account_identity="normal", session_ciphertext="s2"),
                TgAccount(id=103, tenant_id=1, pool_id=1, display_name="账号3", phone_masked="103", status=AccountStatus.ACTIVE.value, account_identity="normal", session_ciphertext="s3"),
            ]
        )
        db.add(
            BotProtocolSample(
                tenant_id=1,
                bot_username="jisou",
                sample_type="search_results",
                sample_hash="sample-hash",
                schema_version="v1",
                structure_json={"buttons": [{"effect": "join_candidate"}]},
                pii_scrubbed=True,
                is_active=True,
            )
        )
        db.commit()
        yield db


def _task(**overrides) -> Task:
    config = {
        "target_operation_target_id": 17,
        "execution_mode": "mtproto_userbot",
        "search_bots": [{"username": "jisou", "display_name": "极搜"}],
        "keyword_hashes": ["a" * 64, "b" * 64],
        "keyword_text_ciphertexts": [encrypt_secret("上海 留学"), encrypt_secret("上海 国际学校")],
        "business_region": "CN-SH",
        "account_locale": "zh-CN",
        "proxy_country": "SG",
        "pre_join_decoy_click_max": 2,
        "post_join_safe_navigation_max": 0,
        "hourly_round_curve": [1] * 24,
        "actions_per_round": 2,
        "max_actions_per_hour": 4,
        "hourly_min_successful_joins": 2,
        "target_relevance_score": 80,
        "target_content_health": "healthy",
        "jisou_ecosystem_status": "bot_joined",
        "paid_keyword_ad_status": "none",
        "post_join_policy": "stay_joined",
    }
    config.update(overrides.pop("type_config", {}))
    return Task(tenant_id=1, name="搜索入群", type="search_join_group", status="running", type_config=config, stats={}, **overrides)


@pytest.mark.no_postgres
def _bind_search_join_environment(session: Session, account_ids: list[int]) -> None:
    session.add(TelegramDeveloperApp(id=51, app_name="TG运营", api_id=2040, api_hash_ciphertext="hash"))
    for index, account_id in enumerate(account_ids, start=1):
        proxy_id = 30 + index
        auth_id = 500 + account_id
        proxy_binding_id = 700 + account_id
        identity = f"identity-{account_id}"
        session.add(AccountProxy(id=proxy_id, tenant_id=1, name=f"airport-clash-{index:03d}", port=7800 + index, status="healthy", alert_status="normal"))
        account = session.get(TgAccount, account_id)
        account.proxy_id = proxy_id
        session.add(
            TgAccountAuthorization(
                id=auth_id,
                tenant_id=1,
                account_id=account_id,
                role="primary",
                developer_app_id=51,
                developer_app_api_id_snapshot=2040,
                proxy_id=proxy_id,
                session_ciphertext=f"session-{account_id}",
                status="active",
                health_status="healthy",
                is_current=True,
            )
        )
        session.add(
            AccountProxyBinding(
                id=proxy_binding_id,
                tenant_id=1,
                account_id=account_id,
                developer_app_id=51,
                developer_app_api_id_snapshot=2040,
                authorization_id=auth_id,
                session_role="primary",
                proxy_id=proxy_id,
            )
        )
        session.add(
            AccountEnvironmentBinding(
                id=f"env-{account_id}",
                tenant_id=1,
                account_id=account_id,
                developer_app_id=51,
                developer_app_api_id_snapshot=2040,
                authorization_id=auth_id,
                session_role="primary",
                proxy_binding_id=proxy_binding_id,
                proxy_id=proxy_id,
                device_model=f"iPhone {index + 13}",
                system_version="iOS 17.5",
                app_version="10.14.1",
                platform="ios",
                client_identity_key=identity,
            )
        )
        session.add(
            FingerprintComboHistory(
                tenant_id=1,
                account_id=account_id,
                developer_app_id=51,
                developer_app_api_id_snapshot=2040,
                authorization_id=auth_id,
                session_role="primary",
                combo_key=identity,
                device_model=f"iPhone {index + 13}",
                system_version="iOS 17.5",
                app_version="10.14.1",
                platform="ios",
                usage_count=1,
            )
        )


def _bind_search_join_authorization_without_environment(session: Session, account_id: int) -> None:
    session.add(TelegramDeveloperApp(id=51, app_name="TG运营", api_id=2040, api_hash_ciphertext="hash"))
    proxy_id = 31
    session.add(AccountProxy(id=proxy_id, tenant_id=1, name="airport-clash-001", port=7801, status="healthy", alert_status="normal"))
    account = session.get(TgAccount, account_id)
    account.proxy_id = proxy_id
    session.add(
        TgAccountAuthorization(
            id=500 + account_id,
            tenant_id=1,
            account_id=account_id,
            role="primary",
            developer_app_id=51,
            developer_app_api_id_snapshot=2040,
            proxy_id=proxy_id,
            session_ciphertext=f"session-{account_id}",
            status="active",
            health_status="healthy",
            is_current=True,
        )
    )


@pytest.mark.no_postgres
def test_search_join_planner_creates_hash_only_search_join_actions(session: Session) -> None:
    _bind_search_join_environment(session, [101, 102])
    task = _task()
    session.add(task)
    session.commit()

    assert build_task_plan(session, task) == 2
    actions = session.scalars(select(Action).where(Action.task_id == task.id)).all()

    assert {action.action_type for action in actions} == {"search_join"}
    assert {action.account_id for action in actions} == {101, 102}
    assert all(action.payload["keyword_hash"] in {"a" * 64, "b" * 64} for action in actions)
    assert all(action.payload["keyword_text_ciphertext"].startswith("enc:v2:") for action in actions)
    assert all("keyword" not in action.payload for action in actions)
    assert all(action.payload["max_pages"] == 70 for action in actions)
    assert "上海 留学" not in str([action.payload for action in actions])
    assert all(action.payload["safe_navigation"]["total_max"] == 2 for action in actions)
    assert all(action.payload["safe_navigation"]["decoy_join_enabled"] is False for action in actions)
    assert actions[0].payload["search_visibility_attribution"]["target_content_health"] == "healthy"
    assert {action.payload["runtime_environment"]["proxy_egress_guard"] for action in actions} == {"verified"}
    assert all(action.payload["authorization_id"] for action in actions)
    assert all(action.payload["session_role"] == "primary" for action in actions)
    assert {action.payload["runtime_environment"]["developer_app_id"] for action in actions} == {"51"}
    assert {action.payload["runtime_environment"]["developer_app_api_id"] for action in actions} == {"2040"}
    assert all(action.payload["target_title"] == "上海群" for action in actions)
    assert all(action.payload["target_peer_id"] == "-10017" for action in actions)
    assert all(action.payload["client_metadata"]["device_model"] for action in actions)
    assert all(action.payload["client_metadata"]["app_version"] for action in actions)
    assert session.query(AccountEnvironmentBinding).count() == 2
    action_authorizations = {action.account_id: action.payload["authorization_id"] for action in actions}
    proxy_bindings = session.query(AccountProxyBinding).order_by(AccountProxyBinding.account_id).all()
    assert [(row.account_id, row.developer_app_id, row.authorization_id, row.session_role) for row in proxy_bindings] == [
        (101, 51, action_authorizations[101], "primary"),
        (102, 51, action_authorizations[102], "primary"),
    ]
    assert session.query(FingerprintComboHistory).count() == 2


@pytest.mark.no_postgres
def test_search_join_planner_caps_new_actions_by_target_count_and_held_slots(session: Session) -> None:
    _bind_search_join_environment(session, [101, 102])
    task = _task(type_config={"target_count": 3})
    session.add(task)
    session.flush()
    session.add_all([
        Action(
            tenant_id=1,
            task_id=task.id,
            task_type=task.type,
            action_type="search_join",
            status="success",
            payload={},
            result={"join_status": "membership_observed"},
        ),
        Action(
            tenant_id=1,
            task_id=task.id,
            task_type=task.type,
            action_type="search_join",
            status="unknown_after_send",
            payload={},
            result={},
        ),
    ])
    session.commit()

    assert build_task_plan(session, task) == 1
    assert session.query(Action).filter_by(task_id=task.id, action_type="search_join").count() == 3
    assert task.stats["search_click_target"]["remaining_slot_count"] == 0


@pytest.mark.no_postgres
def test_search_join_planner_blocks_peer_only_target(session: Session) -> None:
    _bind_search_join_environment(session, [101])
    target = session.get(OperationTarget, 17)
    target.username = ""
    task = _task(type_config={"actions_per_round": 1, "hourly_min_successful_joins": 1})
    session.add(task)
    session.commit()

    assert build_task_plan(session, task) == 0
    assert session.scalar(select(Action).where(Action.task_id == task.id)) is None
    assert "可验证 username" in (task.last_error or "")


@pytest.mark.no_postgres
def test_search_join_planner_marks_verified_proxy_guard(session: Session) -> None:
    _bind_search_join_environment(session, [101, 102])
    task = _task()
    session.add(task)
    session.commit()

    assert build_task_plan(session, task) == 2
    actions = session.scalars(select(Action).where(Action.task_id == task.id)).all()

    assert {action.payload["runtime_environment"]["proxy_egress_guard"] for action in actions} == {"verified"}
    assert {action.payload["runtime_environment"]["client_metadata_guard"] for action in actions} == {"verified"}
    assert {action.payload["runtime_environment"]["proxy_id"] for action in actions} == {"31", "32"}


@pytest.mark.no_postgres
def test_search_join_environment_keeps_fingerprint_and_slot_proxy_when_authorization_proxy_changes(session: Session) -> None:
    _bind_search_join_environment(session, [101])
    task = _task(type_config={"actions_per_round": 1, "hourly_min_successful_joins": 1})
    session.add(task)
    session.commit()

    assert build_task_plan(session, task) == 1
    binding = session.scalar(select(AccountEnvironmentBinding).where(AccountEnvironmentBinding.account_id == 101))
    original_identity = binding.client_identity_key

    session.add(AccountProxy(id=99, tenant_id=1, name="airport-clash-new", port=7890, status="healthy", alert_status="normal"))
    account = session.get(TgAccount, 101)
    authorization = session.scalar(select(TgAccountAuthorization).where(TgAccountAuthorization.account_id == 101))
    account.proxy_id = 99
    authorization.proxy_id = 99
    next_task = _task(id=2, type_config={"actions_per_round": 1, "hourly_min_successful_joins": 1})
    session.add(next_task)
    session.commit()

    assert build_task_plan(session, next_task) == 1
    session.refresh(binding)
    action = session.scalar(select(Action).where(Action.task_id == next_task.id))

    assert binding.client_identity_key == original_identity
    assert binding.developer_app_id == 51
    assert binding.developer_app_api_id_snapshot == 2040
    assert binding.proxy_id == 31
    assert action.payload["runtime_environment"]["proxy_id"] == "31"
    assert action.payload["client_metadata"]["client_identity_key"] == original_identity


@pytest.mark.no_postgres
def test_search_join_environment_reuses_legacy_binding_after_app_scope_added(session: Session) -> None:
    _bind_search_join_authorization_without_environment(session, 101)
    authorization = session.scalar(select(TgAccountAuthorization).where(TgAccountAuthorization.account_id == 101))
    legacy_binding = AccountEnvironmentBinding(
        tenant_id=1,
        account_id=101,
        authorization_id=authorization.id,
        session_role="primary",
        proxy_binding_id=301,
        proxy_id=31,
        device_model="iPhone 15",
        system_version="iOS 17.5",
        app_version="10.14.1",
        platform="ios",
        client_identity_key="legacy-identity-101",
    )
    task = _task(type_config={"actions_per_round": 1, "hourly_min_successful_joins": 1})
    session.add_all([legacy_binding, task])
    session.commit()

    assert build_task_plan(session, task) == 1
    action = session.scalar(select(Action).where(Action.task_id == task.id))
    session.refresh(legacy_binding)

    assert session.query(AccountEnvironmentBinding).count() == 1
    assert legacy_binding.developer_app_id == 51
    assert legacy_binding.developer_app_api_id_snapshot == 2040
    assert action.payload["client_metadata"]["client_identity_key"] == "legacy-identity-101"


@pytest.mark.no_postgres
def test_search_join_planner_fails_closed_without_authorization_environment(session: Session) -> None:
    task = _task()
    session.add(task)
    session.commit()

    assert build_task_plan(session, task) == 0
    assert session.scalar(select(Action).where(Action.task_id == task.id)) is None
    assert task.stats["search_join_stats"]["hourly_execution"]["last_blockers"] == {"needs_client_metadata": 1}


@pytest.mark.no_postgres
def test_search_join_planner_fails_closed_when_all_enabled_clash_subscriptions_unavailable(session: Session) -> None:
    _bind_search_join_environment(session, [101])
    subscription = create_proxy_airport_subscription(
        session,
        tenant_id=1,
        payload=ProxyAirportSubscriptionCreate(
            name="primary",
            subscription_url="https://primary.example.com/sub",
            priority=10,
            enabled=True,
        ),
        actor="tester",
    )
    sync_proxy_airport_subscription_by_id(
        session,
        tenant_id=1,
        subscription_id=subscription.id or 0,
        actor="tester",
        fetcher=lambda _url: "trojan://secret@primary.example.com:443#primary-node",
        health_checker=lambda _node: (False, "connect_timeout"),
    )
    task = _task()
    session.add(task)
    session.commit()

    assert build_task_plan(session, task) == 0
    assert session.scalar(select(Action).where(Action.task_id == task.id)) is None
    assert task.stats["search_join_stats"]["hourly_execution"]["last_blockers"] == {
        "airport_all_subscriptions_unavailable": 1
    }


@pytest.mark.no_postgres
def test_search_join_planner_notifies_admins_when_all_enabled_clash_subscriptions_unavailable(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[tuple[str, str, str]] = []
    tenant = session.get(Tenant, 1)
    tenant.admin_chat_id = "1001,1002"
    tenant.telegram_bot_token_ciphertext = encrypt_secret("bot-token")
    monkeypatch.setattr(
        search_join_executor,
        "send_telegram_bot_message",
        lambda bot_token, chat_id, text: sent.append((bot_token, chat_id, text)) or search_join_executor.NotificationResult(True, "sent"),
    )
    _bind_search_join_environment(session, [101])
    subscription = create_proxy_airport_subscription(
        session,
        tenant_id=1,
        payload=ProxyAirportSubscriptionCreate(
            name="primary",
            subscription_url="https://primary.example.com/sub?token=secret",
            priority=10,
            enabled=True,
        ),
        actor="tester",
    )
    sync_proxy_airport_subscription_by_id(
        session,
        tenant_id=1,
        subscription_id=subscription.id or 0,
        actor="tester",
        fetcher=lambda _url: "trojan://secret@primary.example.com:443#primary-node",
        health_checker=lambda _node: (False, "connect_timeout"),
    )
    task = _task()
    session.add(task)
    session.commit()

    assert build_task_plan(session, task) == 0

    hourly = task.stats["search_join_stats"]["hourly_execution"]
    assert hourly["admin_notification_status"] == "sent"
    assert [chat_id for _token, chat_id, _text in sent] == ["1001", "1002"]
    assert all("secret" not in text for _token, _chat_id, text in sent)


@pytest.mark.no_postgres
def test_search_join_planner_does_not_auto_create_missing_environment_binding(session: Session) -> None:
    _bind_search_join_authorization_without_environment(session, 101)
    task = _task(type_config={"actions_per_round": 1, "hourly_min_successful_joins": 1})
    session.add(task)
    session.commit()

    assert build_task_plan(session, task) == 0
    assert session.scalar(select(Action).where(Action.task_id == task.id)) is None
    assert session.query(AccountEnvironmentBinding).count() == 0
    assert session.query(FingerprintComboHistory).count() == 0
    assert task.stats["search_join_stats"]["hourly_execution"]["last_blockers"] == {"needs_client_metadata": 1}


@pytest.mark.no_postgres
def test_search_join_planner_respects_hourly_success_deficit(session: Session) -> None:
    _bind_search_join_environment(session, [101])
    task = _task()
    session.add(task)
    session.flush()
    session.add(
        Action(
            tenant_id=1,
            task_id=task.id,
            task_type="search_join_group",
            action_type="search_join",
            account_id=201,
            status="success",
            executed_at=_now(),
            payload={"keyword_hash": "a" * 64},
            result={"success": True},
        )
    )
    session.commit()

    assert build_task_plan(session, task) == 1
    stats = task.stats["search_join_stats"]["hourly_execution"]

    assert stats["success_count"] == 1
    assert stats["deficit"] == 1
    assert stats["last_planned_count"] == 1


@pytest.mark.no_postgres
def test_search_join_planner_fails_closed_without_protocol_sample(session: Session) -> None:
    session.query(BotProtocolSample).delete()
    task = _task()
    session.add(task)
    session.commit()

    assert build_task_plan(session, task) == 0
    assert session.scalar(select(Action).where(Action.task_id == task.id)) is None
    assert task.last_error == "search_join protocol sample missing: jisou"
    assert task.stats["search_join_stats"]["hourly_execution"]["last_blockers"] == {"protocol_sample_missing": 1}


@pytest.mark.no_postgres
def test_search_join_planner_fails_closed_without_keyword_hash(session: Session) -> None:
    task = _task(type_config={"keyword_hashes": []})
    session.add(task)
    session.commit()

    assert build_task_plan(session, task) == 0
    assert session.scalar(select(Action).where(Action.task_id == task.id)) is None
    assert task.last_error == "search_join keyword hash/ciphertext material missing or mismatched"


@pytest.mark.no_postgres
def test_search_join_planner_repairs_legacy_duplicate_keyword_ciphertexts() -> None:
    keyword = "上海 留学"
    keyword_hash = normalized_keyword_hash(keyword)
    ciphertext = encrypt_secret(keyword)

    materials = search_join_executor._keyword_materials({
        "keyword_hashes": [keyword_hash],
        "keyword_text_ciphertexts": [ciphertext, ciphertext],
    })

    assert materials == [(keyword_hash, ciphertext)]


@pytest.mark.no_postgres
def test_search_join_planner_persists_daily_skip_decision(session: Session) -> None:
    _bind_search_join_environment(session, [101])
    task = _task(pacing_config={"daily_skip_probability": 1})
    session.add(task)
    session.commit()

    assert build_task_plan(session, task) == 0
    assert build_task_plan(session, task) == 0

    decisions = session.scalars(select(SearchJoinPacingDecision).where(SearchJoinPacingDecision.task_id == task.id)).all()
    assert len(decisions) == 1
    assert decisions[0].decision_scope == "daily"
    assert decisions[0].decision_value["skipped"] is True
    assert session.scalar(select(Action).where(Action.task_id == task.id)) is None
    limits = task.stats["search_join_stats"]["pacing_limits"]
    assert limits["daily_skipped_by_pacing"] == 1


@pytest.mark.no_postgres
def test_search_join_planner_respects_account_and_keyword_daily_limits(session: Session) -> None:
    _bind_search_join_environment(session, [101, 102, 103])
    task = _task(
        type_config={"actions_per_round": 3, "hourly_min_successful_joins": 3},
        pacing_config={"per_account_daily_action_limit": 1, "per_keyword_account_daily_limit": 1},
    )
    session.add(task)
    session.flush()
    session.add(
        Action(
            tenant_id=1,
            task_id=task.id,
            task_type="search_join_group",
            action_type="search_join",
            account_id=101,
            status="success",
            executed_at=_now(),
            payload={"keyword_hash": "a" * 64},
            result={"membership_observed": True},
        )
    )
    session.commit()

    assert build_task_plan(session, task) == 2
    actions = session.scalars(select(Action).where(Action.task_id == task.id, Action.status == "pending")).all()

    assert {action.account_id for action in actions} == {102, 103}
    assert {action.payload["keyword_hash"] for action in actions} == {"a" * 64, "b" * 64}
    limits = task.stats["search_join_stats"]["pacing_limits"]
    assert limits["per_account_daily_limit_reached"] == 1
    assert limits["per_keyword_account_daily_limit_reached"] == 0


@pytest.mark.no_postgres
def test_search_join_planner_tries_next_keyword_when_account_keyword_limit_is_hit(session: Session) -> None:
    _bind_search_join_environment(session, [101])
    task = _task(
        type_config={"actions_per_round": 1, "hourly_min_successful_joins": 2},
        pacing_config={"per_account_daily_action_limit": 2, "per_keyword_account_daily_limit": 1},
    )
    session.add(task)
    session.flush()
    session.add(
        Action(
            tenant_id=1,
            task_id=task.id,
            task_type="search_join_group",
            action_type="search_join",
            account_id=101,
            status="success",
            executed_at=_now(),
            payload={"keyword_hash": "a" * 64},
            result={"membership_observed": True},
        )
    )
    session.commit()

    assert build_task_plan(session, task) == 1
    action = session.scalar(select(Action).where(Action.task_id == task.id, Action.status == "pending"))

    assert action.account_id == 101
    assert action.payload["keyword_hash"] == "b" * 64
    assert task.stats["search_join_stats"]["pacing_limits"]["per_keyword_account_daily_limit_reached"] == 1


@pytest.mark.no_postgres
def test_search_join_planner_respects_account_total_limit_and_cooldown(session: Session) -> None:
    _bind_search_join_environment(session, [101, 102, 103])
    task = _task(pacing_config={"per_account_total_action_limit": 1, "per_account_cooldown_days": 2})
    session.add(task)
    session.flush()
    session.add_all(
        [
            Action(
                tenant_id=1,
                task_id=task.id,
                task_type="search_join_group",
                action_type="search_join",
                account_id=101,
                status="success",
                executed_at=_now() - timedelta(days=3),
                payload={"keyword_hash": "a" * 64},
                result={"membership_observed": True},
            ),
            Action(
                tenant_id=1,
                task_id=task.id,
                task_type="search_join_group",
                action_type="search_join",
                account_id=102,
                status="success",
                executed_at=_now() - timedelta(hours=12),
                payload={"keyword_hash": "b" * 64},
                result={"membership_observed": True},
            ),
        ]
    )
    session.commit()

    assert build_task_plan(session, task) == 1
    action = session.scalar(select(Action).where(Action.task_id == task.id, Action.status == "pending"))

    assert action.account_id == 103
    limits = task.stats["search_join_stats"]["pacing_limits"]
    assert limits["per_account_total_limit_reached"] == 2
    assert limits["per_account_cooldown_days_active"] == 1


@pytest.mark.no_postgres
def test_search_join_planner_creates_explicit_skipped_action_for_action_skip(session: Session) -> None:
    _bind_search_join_environment(session, [101])
    task = _task(type_config={"actions_per_round": 1, "hourly_min_successful_joins": 1}, pacing_config={"skip_probability_per_action": 1})
    session.add(task)
    session.commit()

    assert build_task_plan(session, task) == 1
    action = session.scalar(select(Action).where(Action.task_id == task.id))

    assert action.status == "skipped"
    assert action.result["skip_reason"] == "skipped_by_behavior_pacing"
    decision = session.scalar(select(SearchJoinPacingDecision).where(SearchJoinPacingDecision.task_id == task.id))
    assert decision.decision_scope == "action"
    assert decision.decision_value["skipped"] is True
    assert decision.account_id == 101
    assert decision.keyword_hash == "a" * 64
    assert decision.threshold == 1
    assert decision.reason == "skipped_by_behavior_pacing"

    assert build_task_plan(session, task) == 1
    actions = session.scalars(select(Action).where(Action.task_id == task.id)).all()
    assert len(actions) == 1


@pytest.mark.no_postgres
def test_search_join_planner_applies_jitter_from_persisted_decision(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    fixed_now = _now().replace(year=2026, month=7, day=4, hour=10, minute=0, second=0, microsecond=0)
    monkeypatch.setattr(search_join_executor, "_now", lambda: fixed_now)
    _bind_search_join_environment(session, [101])
    task = _task(
        type_config={"actions_per_round": 1, "hourly_min_successful_joins": 1},
        pacing_config={"hourly_jitter_percent": 30, "daily_jitter_percent": 20, "skip_probability_per_action": 0},
    )
    session.add(task)
    session.commit()

    assert build_task_plan(session, task) == 1
    action = session.scalar(select(Action).where(Action.task_id == task.id))
    decision = session.scalar(select(SearchJoinPacingDecision).where(SearchJoinPacingDecision.task_id == task.id))

    assert action.scheduled_at == decision.scheduled_at
    assert action.scheduled_at >= fixed_now
    assert action.scheduled_at < fixed_now + timedelta(minutes=18, seconds=1)
    assert decision.reason == "planned"
    assert decision.decision_value["hourly_jitter_percent"] == 30
    assert decision.decision_value["daily_jitter_percent"] == 20


@pytest.mark.no_postgres
def test_search_join_jitter_stays_inside_current_hour_bucket(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    fixed_now = _now().replace(year=2026, month=7, day=4, hour=10, minute=50, second=0, microsecond=0)
    monkeypatch.setattr(search_join_executor, "_now", lambda: fixed_now)
    _bind_search_join_environment(session, [101])
    task = _task(
        type_config={"actions_per_round": 1, "hourly_min_successful_joins": 1},
        pacing_config={"hourly_jitter_percent": 100, "daily_jitter_percent": 100, "skip_probability_per_action": 0},
    )
    session.add(task)
    session.commit()

    assert build_task_plan(session, task) == 1
    action = session.scalar(select(Action).where(Action.task_id == task.id))

    assert action.scheduled_at >= fixed_now
    assert action.scheduled_at < fixed_now.replace(minute=0) + timedelta(hours=1)


@pytest.mark.no_postgres
def test_search_join_realtime_pacing_does_not_import_llm_hot_path() -> None:
    executor = (PROJECT_ROOT / "backend/app/services/task_center/executors/search_join_group.py").read_text()
    pacing = (PROJECT_ROOT / "backend/app/services/task_center/search_join_pacing.py").read_text()

    assert "ai_generator" not in executor
    assert "create_ai_gateway" not in executor
    assert "ai_gateway" not in pacing
    assert "LLM" not in executor


@pytest.mark.no_postgres
def test_search_join_planner_locks_task_before_counting_capacity() -> None:
    executor = (PROJECT_ROOT / "backend/app/services/task_center/executors/search_join_group.py").read_text()

    assert "def _lock_task_for_planning" in executor
    assert ".with_for_update()" in executor
    assert "task_daily_capacity(session, task, window" in executor


@pytest.mark.no_postgres
def test_search_join_pacing_window_uses_task_timezone() -> None:
    task = _task(timezone="America/Los_Angeles")

    window = pacing_window(task, _now().replace(year=2026, month=7, day=4, hour=1, minute=30, second=0, microsecond=0))

    assert window.local_date.isoformat() == "2026-07-03"


@pytest.mark.no_postgres
def test_search_join_daily_cap_counts_actions_by_task_timezone(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    fixed_now = _now().replace(year=2026, month=7, day=4, hour=1, minute=30, second=0, microsecond=0)
    monkeypatch.setattr(search_join_executor, "_now", lambda: fixed_now)
    _bind_search_join_environment(session, [101])
    task = _task(timezone="America/Los_Angeles", pacing_config={"max_actions_per_day": 1})
    session.add(task)
    session.flush()
    session.add(
        Action(
            tenant_id=1,
            task_id=task.id,
            task_type="search_join_group",
            action_type="search_join",
            account_id=101,
            status="success",
            executed_at=fixed_now,
            payload={"keyword_hash": "a" * 64},
            result={"membership_observed": True},
        )
    )
    session.commit()

    assert build_task_plan(session, task) == 0
    limits = task.stats["search_join_stats"]["pacing_limits"]
    assert limits["tenant_timezone"] == "America/Los_Angeles"
    assert limits["local_date"] == "2026-07-03"
    assert limits["task_daily_action_count"] == 1
    assert limits["task_daily_remaining"] == 0


@pytest.mark.no_postgres
def test_search_join_daily_limits_count_failed_and_claiming_real_actions(session: Session) -> None:
    _bind_search_join_environment(session, [101, 102])
    task = _task(pacing_config={"per_account_daily_action_limit": 1, "max_actions_per_day": 2})
    session.add(task)
    session.flush()
    session.add_all(
        [
            Action(
                tenant_id=1,
                task_id=task.id,
                task_type="search_join_group",
                action_type="search_join",
                account_id=101,
                status="failed",
                executed_at=_now(),
                payload={"keyword_hash": "a" * 64, "lifecycle_phase": "telegram_search_sent"},
                result={"success": False},
            ),
            Action(
                tenant_id=1,
                task_id=task.id,
                task_type="search_join_group",
                action_type="search_join",
                account_id=102,
                status="claiming",
                scheduled_at=_now(),
                payload={"keyword_hash": "b" * 64},
                result={},
            ),
        ]
    )
    session.commit()

    assert build_task_plan(session, task) == 0
    limits = task.stats["search_join_stats"]["pacing_limits"]
    assert limits["task_daily_action_count"] == 2
    assert limits["task_daily_remaining"] == 0

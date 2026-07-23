from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, select
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
    SchedulingSetting,
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
from app.services.task_center import pacing
from app.services.task_center import search_join_pacing
from app.services.task_center import hourly_stats
from app.services.task_center.jisou_selector_accounts import select_jisou_selector_candidates
from app.services.task_center import search_click_target_progress as search_click_progress
from app.services.task_center.search_join_pacing import pacing_window
from app.services.task_center.executors import build_task_plan
from app.services.task_center.executors import search_join_group as search_join_executor
from app.services.task_center.stats import next_run_after_task

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MAX_REPEAT_SOURCE_ACTION_SELECTS = 120


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


def _search_join_source_action(
    task: Task,
    account_id: int,
    *,
    status: str,
    result: dict,
    executed_at: datetime,
    bot_username: str = "jisou",
) -> Action:
    return Action(
        tenant_id=task.tenant_id,
        task_id=task.id,
        task_type=task.type,
        action_type="search_join",
        account_id=account_id,
        status=status,
        executed_at=executed_at,
        payload={"bot_username": bot_username},
        result=result,
    )


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
    assert all("max_pages" not in action.payload for action in actions)
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
def test_search_join_planner_excludes_selector_failed_jisou_accounts_and_prefers_verified_accounts(
    session: Session,
) -> None:
    _bind_search_join_environment(session, [101, 102, 103])
    task = _task(
        account_config={"selection_mode": "manual", "account_ids": [101, 102, 103], "max_concurrent": 3},
        type_config={"actions_per_round": 2, "hourly_min_successful_joins": 3},
    )
    session.add(task)
    session.flush()
    observed_at = _now()
    session.add_all([
        _search_join_source_action(
            task, 101,
            status="failed",
            executed_at=observed_at,
            result={"error_code": "jisou_group_selector_missing"},
        ),
        _search_join_source_action(
            task, 102,
            status="success",
            executed_at=observed_at,
            result={"target_click_observed": True, "target_found_at": observed_at.isoformat()},
        ),
    ])
    session.commit()

    candidates = select_jisou_selector_candidates(
        session,
        task,
        [session.get(TgAccount, account_id) for account_id in (101, 102, 103)],
        bot_username="jisou",
        now_value=observed_at,
    )
    assert [account.id for account in candidates.accounts] == [102, 103]

    assert build_task_plan(session, task) == 2

    actions = list(session.scalars(
        select(Action)
        .where(Action.task_id == task.id, Action.status == "pending")
        .order_by(Action.id)
    ))
    assert {action.account_id for action in actions} == {102, 103}
    assert task.stats["search_join_stats"]["hourly_execution"]["last_blockers"] == {
        "jisou_group_selector_account_excluded": 1
    }


@pytest.mark.no_postgres
def test_jisou_selector_candidates_ignore_other_bot_outcomes(session: Session) -> None:
    task = _task()
    session.add(task)
    session.flush()
    observed_at = _now()
    session.add(_search_join_source_action(
        task, 101,
        status="failed",
        executed_at=observed_at,
        result={"error_code": "jisou_group_selector_missing"},
        bot_username="soso",
    ))
    session.commit()

    candidates = select_jisou_selector_candidates(
        session,
        task,
        [session.get(TgAccount, 101)],
        bot_username="jisou",
        now_value=observed_at,
    )
    assert [account.id for account in candidates.accounts] == [101]


@pytest.mark.no_postgres
def test_search_join_planner_fails_closed_when_all_jisou_accounts_lack_group_selector(session: Session) -> None:
    _bind_search_join_environment(session, [101, 102])
    task = _task(account_config={"selection_mode": "manual", "account_ids": [101, 102], "max_concurrent": 2})
    session.add(task)
    session.flush()
    observed_at = _now()
    for account_id in (101, 102):
        session.add(_search_join_source_action(
            task, account_id,
            status="failed",
            executed_at=observed_at,
            result={"error_code": "jisou_group_selector_missing"},
        ))
    session.commit()

    assert build_task_plan(session, task) == 0
    assert session.scalar(select(Action).where(Action.task_id == task.id, Action.status == "pending")) is None
    assert task.last_error == "极搜群聊 selector 在候选账号上均不可用"
    assert task.stats["search_join_stats"]["hourly_execution"]["last_blockers"] == {
        "jisou_group_selector_account_unavailable": 2
    }


@pytest.mark.no_postgres
def test_search_join_planner_restores_account_after_a_later_verified_jisou_click(session: Session) -> None:
    _bind_search_join_environment(session, [101])
    task = _task(
        account_config={"selection_mode": "manual", "account_ids": [101], "max_concurrent": 1},
        type_config={"actions_per_round": 1, "hourly_min_successful_joins": 2},
    )
    session.add(task)
    session.flush()
    observed_at = _now()
    session.add_all([
        _search_join_source_action(
            task, 101,
            status="failed",
            executed_at=observed_at - timedelta(minutes=1),
            result={"error_code": "jisou_group_selector_missing"},
        ),
        _search_join_source_action(
            task, 101,
            status="success",
            executed_at=observed_at,
            result={"target_click_observed": True, "target_found_at": observed_at.isoformat()},
        ),
    ])
    session.commit()

    assert build_task_plan(session, task) == 1

    action = session.scalar(select(Action).where(Action.task_id == task.id, Action.status == "pending"))
    assert action is not None
    assert action.account_id == 101


@pytest.mark.no_postgres
def test_search_join_planner_rejects_environment_authorization_from_another_account(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bind_search_join_environment(session, [101, 102])
    wrong_environment = search_join_executor.ensure_search_join_environment(session, session.get(TgAccount, 102))
    task = _task(
        account_config={"selection_mode": "manual", "account_ids": [101], "max_concurrent": 1},
        type_config={"actions_per_round": 1, "hourly_min_successful_joins": 1},
    )
    session.add(task)
    session.commit()
    monkeypatch.setattr(search_join_executor, "ensure_search_join_environment", lambda *_args: wrong_environment)

    assert build_task_plan(session, task) == 0
    assert session.scalar(select(Action).where(Action.task_id == task.id)) is None
    assert task.stats["search_join_stats"]["hourly_execution"]["last_blockers"] == {
        "search_join_environment_authorization_scope_mismatch": 1
    }


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
def test_search_join_daily_target_stops_only_the_current_day(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    _bind_search_join_environment(session, [101])
    now_value = datetime(2026, 7, 21, 10, 0, 0)
    monkeypatch.setattr(search_join_executor, "_now", lambda: now_value)
    monkeypatch.setattr(search_click_progress, "beijing_now", lambda: now_value)
    task = _task(
        type_config={
            "daily_target_count": 1,
            "actions_per_round": 1,
            "hourly_min_successful_joins": 1,
        },
        pacing_config={"max_actions_per_day": 1},
    )
    session.add(task)
    session.flush()
    confirmed = Action(
        tenant_id=1,
        task_id=task.id,
        task_type=task.type,
        action_type="search_join",
        status="success",
        payload={},
        result={"join_status": "membership_observed"},
        executed_at=now_value,
    )
    session.add(confirmed)
    session.commit()

    assert build_task_plan(session, task) == 0
    assert task.status == "running"
    assert task.stats["search_click_target"]["state"] == "daily_target_met"

    confirmed.executed_at = now_value - timedelta(days=1)
    session.commit()

    assert build_task_plan(session, task) == 1
    assert session.query(Action).filter_by(task_id=task.id, action_type="search_join").count() == 2


@pytest.mark.no_postgres
def test_search_join_pending_source_carryover_holds_next_day_click_budget(session: Session) -> None:
    now_value = datetime(2026, 7, 23, 0, 5, 0)
    task = _task(
        type_config={"daily_click_target_count": 3},
        pacing_config={"max_actions_per_day": 3},
    )
    session.add(task)
    session.flush()
    session.add(Action(
        id="source-carryover",
        tenant_id=1,
        task_id=task.id,
        task_type=task.type,
        action_type="search_join",
        account_id=101,
        status="pending",
        scheduled_at=now_value - timedelta(minutes=10),
        payload={},
        result={},
    ))
    session.commit()

    progress = search_click_progress.search_click_target_progress(session, task, now_value=now_value)
    pacing_stats = search_join_pacing.PacingStats()
    capacity = search_join_pacing.task_daily_capacity(
        session,
        task,
        pacing_window(task, now_value),
        3,
        pacing_stats,
    )

    assert progress.held_count == 1
    assert progress.remaining_slot_count == 2
    assert capacity == 2
    assert pacing_stats.task_daily_action_count == 1


@pytest.mark.no_postgres
def test_search_join_daily_target_raises_hourly_plan_demand(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    _bind_search_join_environment(session, [101, 102, 103])
    now_value = datetime(2026, 7, 21, 10, 0, 0)
    monkeypatch.setattr(search_join_executor, "_now", lambda: now_value)
    monkeypatch.setattr(search_click_progress, "beijing_now", lambda: now_value)
    task = _task(
        type_config={
            "daily_target_count": 80,
            "hourly_round_curve": [1] * 24,
            "actions_per_round": 3,
            "max_actions_per_hour": 8,
            "hourly_min_successful_joins": 1,
        },
        pacing_config={
            "max_actions_per_day": 80,
            "per_account_daily_action_limit": 1,
            "per_keyword_account_daily_limit": 1,
            "hourly_jitter_percent": 0,
            "daily_jitter_percent": 0,
        },
    )
    session.add(task)
    session.commit()

    assert build_task_plan(session, task) == 3
    hourly = task.stats["search_join_stats"]["hourly_execution"]
    assert hourly["goal"] == 6
    assert hourly["deficit"] == 6


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
def test_search_join_planner_scans_past_daily_limited_accounts(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    now_value = datetime(2026, 7, 21, 10, 0, 0)
    monkeypatch.setattr(search_join_executor, "_now", lambda: now_value)
    monkeypatch.setattr(search_click_progress, "beijing_now", lambda: now_value)
    session.add(
        TgAccount(
            id=104,
            tenant_id=1,
            pool_id=1,
            display_name="账号4",
            phone_masked="104",
            status=AccountStatus.ACTIVE.value,
            account_identity="normal",
            session_ciphertext="s4",
        )
    )
    _bind_search_join_environment(session, [101, 102, 103, 104])
    task = _task(
        type_config={"actions_per_round": 1, "hourly_min_successful_joins": 1},
        pacing_config={"per_account_daily_action_limit": 1},
    )
    session.add(task)
    session.flush()
    session.add_all(
        Action(
            tenant_id=1,
            task_id=task.id,
            task_type="search_join_group",
            action_type="search_join",
            account_id=account_id,
            status="success",
            executed_at=now_value - timedelta(hours=2),
            payload={"keyword_hash": "a" * 64},
            result={"membership_observed": False},
        )
        for account_id in [101, 102, 103]
    )
    session.commit()

    assert build_task_plan(session, task) == 1
    action = session.scalar(select(Action).where(Action.task_id == task.id, Action.status == "pending"))

    assert action is not None
    assert action.account_id == 104
    assert task.stats["search_join_stats"]["pacing_limits"]["per_account_daily_limit_reached"] == 3


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
    assert action.scheduled_at < datetime(2026, 7, 5, 0, 0, 0)
    assert decision.reason == "planned"
    assert decision.decision_value["hourly_jitter_percent"] == 30
    assert decision.decision_value["daily_jitter_percent"] == 20


@pytest.mark.no_postgres
def test_search_join_planner_does_not_create_actions_during_quiet_hours(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    fixed_now = datetime(2026, 7, 4, 3, 0, 0)
    monkeypatch.setattr(search_join_executor, "_now", lambda: fixed_now)
    _bind_search_join_environment(session, [101])
    task = _task(
        type_config={"actions_per_round": 1, "hourly_min_successful_joins": 1},
        pacing_config={"quiet_hours": {"start": "02:00", "end": "08:00"}},
    )
    session.add(task)
    session.commit()

    assert build_task_plan(session, task) == 0
    assert session.scalar(select(Action).where(Action.task_id == task.id)) is None
    assert task.stats["search_join_stats"]["pacing_limits"]["last_limit_reason"] == "quiet_hours_active"


@pytest.mark.no_postgres
def test_search_join_planner_does_not_schedule_jittered_action_inside_quiet_hours(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed_now = datetime(2026, 7, 4, 1, 59, 0)
    monkeypatch.setattr(search_join_executor, "_now", lambda: fixed_now)
    monkeypatch.setattr(
        search_join_executor,
        "planned_action_decision",
        lambda *_args, **_kwargs: SimpleNamespace(
            scheduled_at=fixed_now + timedelta(minutes=31),
            decision_value={"skipped": False},
        ),
    )
    _bind_search_join_environment(session, [101])
    task = _task(
        type_config={"actions_per_round": 1, "hourly_min_successful_joins": 1},
        pacing_config={"quiet_hours": {"start": "02:00", "end": "08:00"}},
    )
    session.add(task)
    session.commit()

    assert build_task_plan(session, task) == 0
    assert session.scalar(select(Action).where(Action.task_id == task.id)) is None
    assert task.stats["search_join_stats"]["hourly_execution"]["last_blockers"]["quiet_hours_active"] == 1


@pytest.mark.no_postgres
def test_search_join_quiet_hours_follow_task_timezone(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    fixed_now = datetime(2026, 7, 4, 18, 0, 0)
    monkeypatch.setattr(search_join_executor, "_now", lambda: fixed_now)
    _bind_search_join_environment(session, [101])
    task = _task(
        timezone="America/New_York",
        type_config={"actions_per_round": 1, "hourly_min_successful_joins": 1},
        pacing_config={"quiet_hours": {"start": "02:00", "end": "08:00"}},
    )
    session.add(task)
    session.commit()

    assert build_task_plan(session, task) == 0
    assert session.scalar(select(Action).where(Action.task_id == task.id)) is None
    assert task.stats["search_join_stats"]["pacing_limits"]["last_limit_reason"] == "quiet_hours_active"


@pytest.mark.no_postgres
def test_search_join_daily_jitter_stays_inside_current_task_local_day(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
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
    assert action.scheduled_at < datetime(2026, 7, 5, 0, 0, 0)


@pytest.mark.no_postgres
def test_search_join_daily_jitter_spreads_within_remaining_task_local_day(monkeypatch: pytest.MonkeyPatch) -> None:
    base = datetime(2026, 7, 4, 10, 0, 0)
    task = _task(timezone="Asia/Shanghai")
    window = pacing_window(task, base)
    monkeypatch.setattr(search_join_pacing, "_seeded_float", lambda *_args: 1.0)

    scheduled_at = search_join_pacing._jittered_at(task, "candidate", base, 0, 100, window)

    assert scheduled_at == datetime(2026, 7, 4, 23, 59, 59)


@pytest.mark.no_postgres
def test_search_join_daily_jitter_keeps_future_hour_action_cap(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    fixed_now = datetime(2026, 7, 4, 10, 0, 0)
    monkeypatch.setattr(search_join_executor, "_now", lambda: fixed_now)
    monkeypatch.setattr(search_join_pacing, "_seeded_float", lambda *_args: 1.0)
    _bind_search_join_environment(session, [101])
    task = _task(
        type_config={"actions_per_round": 1, "hourly_min_successful_joins": 1, "max_actions_per_hour": 1},
        pacing_config={"daily_jitter_percent": 100, "hourly_jitter_percent": 0, "skip_probability_per_action": 0},
    )
    session.add(task)
    session.flush()
    session.add(
        Action(
            tenant_id=1,
            task_id=task.id,
            task_type=task.type,
            action_type="search_join",
            account_id=102,
            scheduled_at=datetime(2026, 7, 4, 23, 59, 59),
            status="pending",
            payload={"keyword_hash": "a" * 64},
            result={},
        )
    )
    session.commit()

    assert build_task_plan(session, task) == 0

    actions = session.scalars(select(Action).where(Action.task_id == task.id)).all()
    assert len(actions) == 1
    assert actions[0].scheduled_at == datetime(2026, 7, 4, 23, 59, 59)


@pytest.mark.no_postgres
def test_search_join_jitter_never_schedules_after_task_deadline(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    fixed_now = _now().replace(year=2026, month=7, day=4, hour=9, minute=59, second=50, microsecond=0)
    deadline = datetime(2026, 7, 4, 2, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(search_join_executor, "_now", lambda: fixed_now)
    _bind_search_join_environment(session, [101])
    task = _task(
        scheduled_end=deadline,
        type_config={"actions_per_round": 1, "hourly_min_successful_joins": 1},
        pacing_config={"hourly_jitter_percent": 100, "daily_jitter_percent": 100, "skip_probability_per_action": 0},
    )
    session.add(task)
    session.commit()
    task.scheduled_end = deadline

    assert build_task_plan(session, task) == 1

    action = session.scalar(select(Action).where(Action.task_id == task.id))
    decision = session.scalar(select(SearchJoinPacingDecision).where(SearchJoinPacingDecision.task_id == task.id))
    assert action.scheduled_at < datetime(2026, 7, 4, 10, 0, 0)
    assert decision.scheduled_at == action.scheduled_at


@pytest.mark.no_postgres
def test_search_click_next_run_exits_quiet_hours_in_task_timezone(monkeypatch: pytest.MonkeyPatch) -> None:
    beijing_now = datetime(2026, 7, 4, 18, 0, 0)
    monkeypatch.setattr(pacing, "_now", lambda: beijing_now)
    task = _task(
        timezone="America/New_York",
        pacing_config={"quiet_hours": {"start": "02:00", "end": "08:00"}},
    )

    assert next_run_after_task(task) == datetime(2026, 7, 4, 20, 0, 0)


@pytest.mark.no_postgres
def test_search_click_next_run_honors_quiet_hours_with_activity_curve(monkeypatch: pytest.MonkeyPatch) -> None:
    beijing_now = datetime(2026, 7, 4, 18, 0, 0)
    monkeypatch.setattr(pacing, "_now", lambda: beijing_now)
    task = _task(
        timezone="America/New_York",
        pacing_config={
            "quiet_hours": {"start": "02:00", "end": "08:00"},
            "operation_profile": {"hourly_activity_curve": [1] * 24},
        },
    )

    assert next_run_after_task(task) == datetime(2026, 7, 4, 20, 0, 0)


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


@pytest.mark.no_postgres
def test_strict_daily_click_target_replaces_terminal_sources_without_click_fact(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed_now = datetime(2026, 7, 23, 10, 0, 0)
    monkeypatch.setattr(search_join_executor, "_now", lambda: fixed_now)
    _bind_search_join_environment(session, [101])
    task = _task(
        account_config={"selection_mode": "manual", "account_ids": [101], "max_concurrent": 1},
        type_config={
            "daily_click_target_count": 2,
            "strict_daily_target": True,
            "allow_same_account_repeat_application": True,
            "hourly_round_curve": [0] * 10 + [1] + [0] * 13,
            "actions_per_round": 2,
            "max_actions_per_hour": 4,
            "hourly_min_successful_joins": 2,
        },
        pacing_config={"max_actions_per_day": 2, "hourly_jitter_percent": 0, "daily_jitter_percent": 0},
    )
    session.add(task)
    session.flush()
    session.add_all([
        Action(
            tenant_id=1,
            task_id=task.id,
            task_type=task.type,
            action_type="search_join",
            account_id=101,
            status="success",
            executed_at=fixed_now,
            payload={},
            result={"search_end_reason": "target_not_found"},
        ),
        Action(
            tenant_id=1,
            task_id=task.id,
            task_type=task.type,
            action_type="search_join",
            account_id=101,
            status="failed",
            executed_at=fixed_now,
            payload={},
            result={"success": False},
        ),
    ])
    session.commit()

    assert build_task_plan(session, task) == 2
    limits = task.stats["search_join_stats"]["pacing_limits"]

    assert session.query(Action).filter_by(task_id=task.id, action_type="search_join").count() == 4
    assert limits["task_daily_action_count"] == 2
    assert limits["task_daily_base_budget"] == 2
    assert limits["terminal_unconfirmed_click_count"] == 2
    assert limits["task_daily_effective_budget"] == 4
    assert limits["task_daily_remaining"] == 2


@pytest.mark.no_postgres
def test_strict_daily_click_target_keeps_unknown_after_send_in_held_budget(session: Session) -> None:
    now_value = datetime(2026, 7, 23, 10, 0, 0)
    task = _task(
        type_config={"daily_click_target_count": 1, "strict_daily_target": True},
        pacing_config={"max_actions_per_day": 1},
    )
    session.add(task)
    session.flush()
    session.add(Action(
        tenant_id=1,
        task_id=task.id,
        task_type=task.type,
        action_type="search_join",
        account_id=101,
        status="unknown_after_send",
        executed_at=now_value,
        payload={},
        result={"gateway_call_state": "started"},
    ))
    session.commit()

    limits = search_join_pacing.PacingStats()
    capacity = search_join_pacing.task_daily_capacity(session, task, pacing_window(task, now_value), 1, limits)

    assert capacity == 0
    assert limits.terminal_unconfirmed_click_count == 0
    assert limits.task_daily_effective_budget == 1


@pytest.mark.no_postgres
def test_search_join_planner_avoids_repeat_join_request_for_same_account(session: Session) -> None:
    _bind_search_join_environment(session, [101, 102])
    task = _task(
        account_config={"selection_mode": "manual", "account_ids": [101, 102], "max_concurrent": 2},
        type_config={"actions_per_round": 1, "hourly_min_successful_joins": 1},
        pacing_config={"per_account_daily_action_limit": 2},
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
            status="failed",
            executed_at=_now(),
            payload={"keyword_hash": "a" * 64},
            result={
                "error_code": "search_join_execution_failed",
                "detail": "You have successfully requested to join this chat or channel (caused by JoinChannelRequest)",
            },
        )
    )
    session.commit()

    assert build_task_plan(session, task) == 1
    action = session.scalar(select(Action).where(Action.task_id == task.id, Action.status == "pending"))

    assert action is not None
    assert action.account_id == 102
    assert task.stats["search_join_stats"]["pacing_limits"]["join_request_pending"] == 1


@pytest.mark.no_postgres
def test_search_join_repeat_application_mode_reuses_the_same_account_within_one_plan(session: Session) -> None:
    _bind_search_join_environment(session, [101])
    task = _task(
        account_config={"selection_mode": "manual", "account_ids": [101], "max_concurrent": 1},
        type_config={
            "daily_click_target_count": 2,
            "daily_target_count": 1,
            "allow_same_account_repeat_application": True,
            "actions_per_round": 2,
            "max_actions_per_hour": 2,
            "hourly_min_successful_joins": 2,
        },
        pacing_config={
            "max_actions_per_day": 2,
            "per_account_daily_action_limit": 1,
            "per_keyword_account_daily_limit": 1,
            "hourly_jitter_percent": 0,
            "daily_jitter_percent": 0,
        },
    )
    session.add(task)
    session.commit()

    assert build_task_plan(session, task) == 2

    actions = list(session.scalars(select(Action).where(Action.task_id == task.id, Action.action_type == "search_join")))
    assert [action.account_id for action in actions] == [101, 101]


@pytest.mark.no_postgres
def test_search_join_planner_defers_repeat_sources_to_global_account_capacity(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed_now = datetime(2026, 7, 23, 10, 0, 0)
    monkeypatch.setattr(search_join_executor, "_now", lambda: fixed_now)
    _bind_search_join_environment(session, [101])
    session.add(
        SchedulingSetting(
            tenant_id=1,
            jitter_min_seconds=0,
            jitter_max_seconds=0,
            default_account_cooldown_seconds=180,
        )
    )
    task = _task(
        account_config={"selection_mode": "manual", "account_ids": [101], "max_concurrent": 1},
        type_config={
            "daily_click_target_count": 2,
            "daily_target_count": 1,
            "allow_same_account_repeat_application": True,
            "actions_per_round": 2,
            "max_actions_per_hour": 2,
            "hourly_min_successful_joins": 2,
        },
        pacing_config={
            "max_actions_per_day": 2,
            "hourly_jitter_percent": 0,
            "daily_jitter_percent": 0,
        },
    )
    session.add(task)
    session.flush()
    session.add(
        Action(
            tenant_id=1,
            task_id=task.id,
            task_type=task.type,
            action_type="send_message",
            account_id=101,
            status="success",
            scheduled_at=fixed_now,
            executed_at=fixed_now,
            payload={},
            result={},
        )
    )
    session.commit()

    assert build_task_plan(session, task) == 2

    actions = list(
        session.scalars(
            select(Action)
            .where(Action.task_id == task.id, Action.action_type == "search_join")
            .order_by(Action.scheduled_at)
        )
    )

    assert [action.scheduled_at for action in actions] == [
        fixed_now + timedelta(seconds=180),
        fixed_now + timedelta(seconds=360),
    ]


@pytest.mark.no_postgres
def test_search_join_repeat_plan_reuses_capacity_cache(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed_now = datetime(2026, 7, 23, 10, 0, 0)
    monkeypatch.setattr(search_join_executor, "_now", lambda: fixed_now)
    _bind_search_join_environment(session, [101])
    session.add(
        SchedulingSetting(
            tenant_id=1,
            jitter_min_seconds=0,
            jitter_max_seconds=0,
            default_account_cooldown_seconds=180,
        )
    )
    task = _task(
        account_config={"selection_mode": "manual", "account_ids": [101], "max_concurrent": 1},
        type_config={
            "daily_click_target_count": 20,
            "daily_target_count": 1,
            "allow_same_account_repeat_application": True,
            "actions_per_round": 20,
            "max_actions_per_hour": 20,
            "hourly_min_successful_joins": 20,
        },
        pacing_config={"max_actions_per_day": 20, "hourly_jitter_percent": 0, "daily_jitter_percent": 0},
    )
    session.add(task)
    session.commit()

    action_selects: list[str] = []
    engine = session.get_bind()

    def record_action_selects(_, __, statement, ___, ____, _____) -> None:
        normalized = statement.upper()
        if statement.lstrip().upper().startswith("SELECT") and ("FROM ACTIONS" in normalized or "FROM MESSAGE_TASKS" in normalized):
            action_selects.append(statement)

    event.listen(engine, "before_cursor_execute", record_action_selects)
    try:
        assert build_task_plan(session, task) == 20
    finally:
        event.remove(engine, "before_cursor_execute", record_action_selects)

    actions = list(session.scalars(select(Action).where(Action.task_id == task.id, Action.action_type == "search_join")))
    assert len(actions) == 20
    assert len(action_selects) <= MAX_REPEAT_SOURCE_ACTION_SELECTS


@pytest.mark.no_postgres
def test_search_join_planner_does_not_defer_source_past_task_deadline(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed_now = datetime(2026, 7, 23, 10, 0, 0)
    monkeypatch.setattr(search_join_executor, "_now", lambda: fixed_now)
    _bind_search_join_environment(session, [101])
    session.add(
        SchedulingSetting(
            tenant_id=1,
            jitter_min_seconds=0,
            jitter_max_seconds=0,
            default_account_cooldown_seconds=180,
        )
    )
    task = _task(
        scheduled_end=fixed_now + timedelta(seconds=120),
        account_config={"selection_mode": "manual", "account_ids": [101], "max_concurrent": 1},
        type_config={"actions_per_round": 1, "hourly_min_successful_joins": 1},
        pacing_config={"max_actions_per_day": 1, "hourly_jitter_percent": 0, "daily_jitter_percent": 0},
    )
    session.add(task)
    session.flush()
    session.add(
        Action(
            tenant_id=1,
            task_id=task.id,
            task_type=task.type,
            action_type="send_message",
            account_id=101,
            status="success",
            scheduled_at=fixed_now,
            executed_at=fixed_now,
            payload={},
            result={},
        )
    )
    session.commit()

    assert build_task_plan(session, task) == 0
    assert not list(session.scalars(select(Action).where(Action.task_id == task.id, Action.action_type == "search_join")))


@pytest.mark.no_postgres
def test_click_only_daily_target_uses_remaining_daily_curve(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    fixed_now = datetime(2026, 7, 23, 23, 30)
    monkeypatch.setattr(search_join_executor, "_now", lambda: fixed_now)
    _bind_search_join_environment(session, [101])
    task = _task(
        account_config={"selection_mode": "manual", "account_ids": [101], "max_concurrent": 1},
        type_config={
            "daily_click_target_count": 50,
            "allow_same_account_repeat_application": True,
            "hourly_round_curve": [0] * 23 + [1],
            "actions_per_round": 20,
            "max_actions_per_hour": 50,
            "hourly_min_successful_joins": 1,
        },
        pacing_config={
            "max_actions_per_day": 50,
            "hourly_jitter_percent": 0,
            "daily_jitter_percent": 0,
        },
    )
    session.add(task)
    session.commit()

    assert build_task_plan(session, task) == 20
    assert task.stats["search_join_stats"]["hourly_execution"]["goal"] == 50


@pytest.mark.no_postgres
def test_click_daily_target_uses_only_deadline_and_quiet_eligible_curve_weight(session: Session) -> None:
    now_value = datetime(2026, 7, 24, 8, 0)
    task = _task(
        timezone="Asia/Shanghai",
        scheduled_end=datetime(2026, 7, 24, 10, 30),
        type_config={
            "daily_click_target_count": 100,
            "hourly_round_curve": [0] * 8 + [1, 1, 1] + [9] * 13,
            "hourly_min_successful_joins": 1,
        },
        pacing_config={"quiet_hours": {"start": "09:00", "end": "10:00"}},
    )
    config = {**task.type_config, **task.pacing_config}
    progress = search_click_progress.SearchClickTargetProgress(
        target_count=100,
        confirmed_count=0,
        held_count=0,
        remaining_slot_count=100,
        scope="daily",
        local_date="2026-07-24",
    )

    assert hourly_stats._remaining_daily_curve_weight(task, config, now_value) == (1, 2)
    assert hourly_stats._search_join_hourly_goal(session, task, config, now_value, progress) == 50


@pytest.mark.no_postgres
def test_search_join_daily_limit_counts_unknown_after_gateway(session: Session) -> None:
    _bind_search_join_environment(session, [101])
    task = _task(pacing_config={"max_actions_per_day": 1})
    session.add(task)
    session.flush()
    session.add(
        Action(
            tenant_id=1,
            task_id=task.id,
            task_type="search_join_group",
            action_type="search_join",
            account_id=101,
            status="unknown_after_send",
            executed_at=_now(),
            payload={"keyword_hash": "a" * 64},
            result={"gateway_call_state": "started"},
        )
    )
    session.commit()

    assert build_task_plan(session, task) == 0
    limits = task.stats["search_join_stats"]["pacing_limits"]
    assert limits["task_daily_action_count"] == 1
    assert limits["task_daily_remaining"] == 0

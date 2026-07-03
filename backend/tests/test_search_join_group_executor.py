from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    AccountEnvironmentBinding,
    AccountProxy,
    AccountStatus,
    Action,
    BotProtocolSample,
    FingerprintComboHistory,
    OperationTarget,
    Task,
    TelegramDeveloperApp,
    Tenant,
    TgAccount,
    TgAccountAuthorization,
)
from app.security import encrypt_secret
from app.services._common import _now
from app.services.task_center.executors import build_task_plan


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Tenant(id=1, name="默认运营空间"))
        db.add(OperationTarget(id=17, tenant_id=1, target_type="group", tg_peer_id="-10017", title="上海群", username="shanghai"))
        db.add_all(
            [
                TgAccount(id=101, tenant_id=1, display_name="账号1", phone_masked="101", status=AccountStatus.ACTIVE.value, session_ciphertext="s1"),
                TgAccount(id=102, tenant_id=1, display_name="账号2", phone_masked="102", status=AccountStatus.ACTIVE.value, session_ciphertext="s2"),
                TgAccount(id=103, tenant_id=1, display_name="账号3", phone_masked="103", status=AccountStatus.ACTIVE.value, session_ciphertext="s3"),
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
        "post_join_safe_navigation_max": 1,
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
        session.add(AccountProxy(id=proxy_id, tenant_id=1, name=f"airport-clash-{index:03d}", port=7800 + index, status="healthy", alert_status="normal"))
        account = session.get(TgAccount, account_id)
        account.proxy_id = proxy_id
        session.add(
            TgAccountAuthorization(
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
    assert all(action.payload["safe_navigation"]["total_max"] == 3 for action in actions)
    assert all(action.payload["safe_navigation"]["decoy_join_enabled"] is False for action in actions)
    assert actions[0].payload["search_visibility_attribution"]["target_content_health"] == "healthy"
    assert {action.payload["runtime_environment"]["proxy_egress_guard"] for action in actions} == {"verified"}
    assert all(action.payload["authorization_id"] for action in actions)
    assert all(action.payload["session_role"] == "primary" for action in actions)
    assert all(action.payload["target_title"] == "上海群" for action in actions)
    assert all(action.payload["target_peer_id"] == "-10017" for action in actions)
    assert all(action.payload["client_metadata"]["device_model"] for action in actions)
    assert all(action.payload["client_metadata"]["app_version"] for action in actions)
    assert session.query(AccountEnvironmentBinding).count() == 2
    assert session.query(FingerprintComboHistory).count() == 2


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
def test_search_join_environment_keeps_fingerprint_and_syncs_proxy_rebind(session: Session) -> None:
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
    assert binding.proxy_id == 99
    assert action.payload["runtime_environment"]["proxy_id"] == "99"
    assert action.payload["client_metadata"]["client_identity_key"] == original_identity


@pytest.mark.no_postgres
def test_search_join_planner_fails_closed_without_authorization_environment(session: Session) -> None:
    task = _task()
    session.add(task)
    session.commit()

    assert build_task_plan(session, task) == 0
    assert session.scalar(select(Action).where(Action.task_id == task.id)) is None
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
    assert task.last_error == "search_join keyword hash missing"

from __future__ import annotations

import hashlib

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import OperationTarget, Task, Tenant, TgAccount
from app.security import decrypt_secret, encrypt_secret
from app.schemas.task_center import (
    SearchJoinGroupSimpleTaskCreate,
    SearchJoinGroupTaskConfigUpdate,
    SearchJoinGroupTaskCreate,
    SearchRankDeboostSimpleTaskCreate,
    TaskSettingsUpdate,
)
from app.services.task_center import service as task_service
from app.services.task_center.service import (
    create_and_start_search_join_group_task,
    create_search_join_group_task,
    create_simple_search_join_group_task,
    update_task_settings,
)


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Tenant(id=1, name="默认运营空间"))
        db.add(
            OperationTarget(
                id=17,
                tenant_id=1,
                target_type="group",
                tg_peer_id="-10017",
                title="上海留学交流群",
                username="shanghai_study_group",
            )
        )
        db.add(
            TgAccount(
                id=101,
                tenant_id=1,
                display_name="搜索账号",
                phone_masked="101",
                status="在线",
                session_ciphertext="session-101",
            )
        )
        db.commit()
        yield db


def _payload(**overrides) -> SearchJoinGroupTaskCreate:
    data = {
        "name": "上海搜索入群",
        "target_operation_target_id": 17,
        "search_bots": [{"username": "jisou", "display_name": "极搜"}],
        "keywords": ["上海 留学", "上海 国际学校"],
        "business_region": "CN-SH",
        "pre_join_decoy_click_min": 1,
        "pre_join_decoy_click_max": 2,
        "post_join_safe_navigation_min": 0,
        "post_join_safe_navigation_max": 0,
        "decoy_join_enabled": False,
        "hourly_min_successful_joins": 2,
    }
    data.update(overrides)
    return SearchJoinGroupTaskCreate(**data)


@pytest.mark.no_postgres
def test_simple_search_join_create_uses_system_name_and_policy(session: Session) -> None:
    task = create_simple_search_join_group_task(
        session,
        1,
        SearchJoinGroupSimpleTaskCreate(
            target_operation_target_id=17,
            keywords=["上海 留学", "上海 国际学校"],
            target_count=12,
        ),
        actor="tester",
    )

    assert task.name == "上海留学交流群 搜索目标群点击 12 次"
    assert task.type_config["target_operation_target_id"] == 17
    assert task.type_config["target_count"] == 12
    assert task.type_config["search_bots"] == [{"username": "jisou", "display_name": "极搜"}]
    assert task.account_config["selection_mode"] == "all"
    assert task.pacing_config["per_account_daily_action_limit"] == 1


@pytest.mark.no_postgres
def test_simple_search_join_edit_regenerates_system_name(session: Session) -> None:
    task = create_simple_search_join_group_task(
        session,
        1,
        SearchJoinGroupSimpleTaskCreate(
            target_operation_target_id=17,
            keywords=["上海 留学"],
            target_count=12,
        ),
        actor="tester",
    )
    session.add(
        OperationTarget(
            id=18,
            tenant_id=1,
            target_type="group",
            tg_peer_id="-10018",
            title="北京留学交流群",
            username="beijing_study_group",
        )
    )
    session.commit()

    target_updated = task_service.update_search_join_group_config(
        session,
        1,
        task.id,
        SearchJoinGroupTaskConfigUpdate(target_operation_target_id=18),
        actor="tester",
    )
    assert target_updated.name == "北京留学交流群 搜索目标群点击 12 次"

    updated = task_service.update_search_join_group_config(
        session,
        1,
        task.id,
        SearchJoinGroupTaskConfigUpdate(target_count=8),
        actor="tester",
    )

    assert updated.name == "北京留学交流群 搜索目标群点击 8 次"


@pytest.mark.no_postgres
def test_simple_search_join_create_rejects_system_managed_fields() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        SearchJoinGroupSimpleTaskCreate(
            target_operation_target_id=17,
            keywords=["上海 留学"],
            target_count=12,
            search_bots=[{"username": "other"}],
        )


@pytest.mark.no_postgres
@pytest.mark.parametrize("payload_type", [SearchJoinGroupSimpleTaskCreate, SearchRankDeboostSimpleTaskCreate])
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", "调用方自定义名称"),
        ("account_config", {"selection_mode": "manual", "account_ids": [101]}),
        ("account_pool_id", 1),
        ("proxy_airport_node_id", 1),
        ("search_bots", ["other_bot"]),
        ("pacing_config", {"max_actions_per_hour": 1}),
        ("dwell_seconds_min", 30),
        ("per_account_daily_action_limit", 1),
        ("retry_policy", {"max_retries": 3}),
        ("risk_config", {"mode": "caller_selected"}),
    ],
)
def test_simple_search_click_create_rejects_all_system_managed_inputs(payload_type, field: str, value: object) -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        payload_type(
            target_operation_target_id=17,
            keywords=["上海 留学"],
            target_count=12,
            **{field: value},
        )


@pytest.mark.no_postgres
def test_simple_search_join_create_deduplicates_normalized_keywords() -> None:
    payload = SearchJoinGroupSimpleTaskCreate(
        target_operation_target_id=17,
        keywords=["  Study   Group  ", "study group"],
        target_count=12,
    )

    assert payload.keywords == ["Study   Group"]


@pytest.mark.no_postgres
def test_simple_search_join_create_does_not_impose_an_unstated_target_count_cap() -> None:
    payload = SearchJoinGroupSimpleTaskCreate(
        target_operation_target_id=17,
        keywords=["上海 留学"],
        target_count=100_001,
    )

    assert payload.target_count == 100_001


@pytest.mark.no_postgres
@pytest.mark.parametrize("username", [" @ ", "bad name", "12345", "abcd", "name-with-dash"])
def test_simple_search_join_create_rejects_target_without_public_username(session: Session, username: str) -> None:
    target = session.get(OperationTarget, 17)
    target.username = username
    session.commit()

    with pytest.raises(ValueError, match="公开 username"):
        create_simple_search_join_group_task(
            session,
            1,
            SearchJoinGroupSimpleTaskCreate(
                target_operation_target_id=17,
                keywords=["上海 留学"],
                target_count=12,
            ),
            actor="tester",
        )


@pytest.mark.no_postgres
def test_search_join_group_create_persists_fixed_mode_and_keyword_hashes(session: Session) -> None:
    payload = _payload(
        pacing_config={
            "mode": "curve",
            "curve_type": "steady",
            "max_actions_per_hour": 4,
            "max_actions_per_day": 6,
            "per_account_total_action_limit": 3,
            "per_account_daily_action_limit": 1,
            "per_account_cooldown_days": 2,
            "per_keyword_account_daily_limit": 1,
            "hourly_skip_probability": 0.25,
            "daily_skip_probability": 0.5,
            "skip_probability_per_action": 0.1,
            "hourly_jitter_percent": 30,
            "daily_jitter_percent": 20,
        }
    )
    task = create_search_join_group_task(session, 1, payload, actor="tester")

    assert task.type == "search_join_group"
    assert task.type_config["execution_mode"] == "mtproto_userbot"
    assert task.type_config["target_operation_target_id"] == 17
    assert task.pacing_config["per_account_daily_action_limit"] == 1
    assert task.pacing_config["per_account_cooldown_days"] == 2
    assert task.pacing_config["per_keyword_account_daily_limit"] == 1
    assert task.pacing_config["hourly_skip_probability"] == 0.25
    assert task.pacing_config["daily_skip_probability"] == 0.5
    assert task.pacing_config["skip_probability_per_action"] == 0.1
    assert task.pacing_config["hourly_jitter_percent"] == 30
    assert task.pacing_config["daily_jitter_percent"] == 20
    keyword_hashes = task.type_config["keyword_hashes"]
    keyword_ciphertexts = task.type_config["keyword_text_ciphertexts"]
    assert len(keyword_hashes) == 2
    assert [decrypt_secret(item) for item in keyword_ciphertexts] == ["上海 留学", "上海 国际学校"]
    assert all(len(item) == 64 for item in keyword_hashes)
    assert "上海 留学" not in str(task.type_config)
    assert "上海 国际学校" not in str(task.type_config)


@pytest.mark.no_postgres
def test_search_join_group_rejects_post_join_navigation_and_decoy_joins() -> None:
    with pytest.raises(ValidationError, match="post_join_safe_navigation 本期不支持"):
        _payload(post_join_safe_navigation_min=1, post_join_safe_navigation_max=1)

    with pytest.raises(ValidationError, match="不得加入非目标群"):
        _payload(decoy_join_enabled=True)


@pytest.mark.no_postgres
def test_search_join_group_requires_keyword_hash_material() -> None:
    with pytest.raises(ValidationError, match="keywords 或 keyword_hashes 至少提供一个"):
        _payload(keywords=[], keyword_hashes=[])


@pytest.mark.no_postgres
def test_search_join_group_rejects_invalid_keyword_hashes() -> None:
    with pytest.raises(ValidationError, match="keyword_hashes 必须是 64 位小写 hex"):
        _payload(
            keywords=[],
            keyword_hashes=["not-a-sha256"],
            keyword_text_ciphertexts=[encrypt_secret("上海 留学")],
        )


@pytest.mark.no_postgres
def test_search_join_group_update_rejects_mismatched_keyword_material() -> None:
    with pytest.raises(ValidationError, match="keyword_hashes 与 keyword_text_ciphertexts 的关键词内容不匹配"):
        SearchJoinGroupTaskConfigUpdate(
            keyword_hashes=[hashlib.sha256("审计关键词".encode("utf-8")).hexdigest()],
            keyword_text_ciphertexts=[encrypt_secret("实际搜索关键词")],
        )


@pytest.mark.no_postgres
def test_search_join_group_rejects_conflicting_legacy_jitter() -> None:
    with pytest.raises(ValidationError, match="jitter_percent 与 hourly_jitter_percent 冲突"):
        _payload(pacing_config={"mode": "curve", "jitter_percent": 10, "hourly_jitter_percent": 30})


@pytest.mark.no_postgres
def test_search_join_group_create_and_start_runs_precheck_and_starts(session: Session) -> None:
    task = create_and_start_search_join_group_task(session, 1, _payload(), actor="tester")

    assert task.status == "running"
    assert task.stats["started_at"]
    assert task.type_config["search_visibility_attribution"]["organic_search_join"] is True


@pytest.mark.no_postgres
def test_search_join_group_settings_update_accepts_pacing_but_other_tasks_reject_it(session: Session) -> None:
    task = create_search_join_group_task(session, 1, _payload(), actor="tester")

    updated = update_task_settings(
        session,
        1,
        task.id,
        TaskSettingsUpdate(name=task.name, pacing_config={"mode": "template", "per_account_daily_action_limit": 0}),
        actor="tester",
    )

    assert updated.pacing_config["per_account_daily_action_limit"] == 0

    stopped_capacity = update_task_settings(
        session,
        1,
        task.id,
        TaskSettingsUpdate(name=task.name, pacing_config={"mode": "template", "max_actions_per_hour": 0}),
        actor="tester",
    )

    assert stopped_capacity.pacing_config["max_actions_per_hour"] == 0

    other = Task(tenant_id=1, name="普通任务", type="channel_like", status="running", type_config={}, stats={})
    session.add(other)
    session.commit()

    generic = update_task_settings(
        session,
        1,
        other.id,
        TaskSettingsUpdate(name=other.name, pacing_config={"mode": "template", "jitter_percent": 0, "max_actions_per_day": 10}),
        actor="tester",
    )

    assert generic.pacing_config["max_actions_per_day"] == 10
    with pytest.raises(ValueError, match="search_join_group 专属 pacing 字段不能用于其他任务类型"):
        update_task_settings(
            session,
            1,
            other.id,
            TaskSettingsUpdate(name=other.name, pacing_config={"mode": "template", "per_account_daily_action_limit": 0}),
            actor="tester",
        )


@pytest.mark.no_postgres
def test_search_join_group_config_update_rolls_back_type_config_when_pacing_fails(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    task = create_search_join_group_task(session, 1, _payload(), actor="tester")
    original_region = task.type_config["business_region"]

    def fail_pacing(_payload):
        raise RuntimeError("pacing boom")

    monkeypatch.setattr(task_service, "pacing_config_payload", fail_pacing)
    with pytest.raises(RuntimeError, match="pacing boom"):
        task_service.update_search_join_group_config(
            session,
            1,
            task.id,
            SearchJoinGroupTaskConfigUpdate(
                target_operation_target_id=17,
                search_bots=[{"username": "jisou", "display_name": "极搜"}],
                business_region="CN-ZZ",
                pacing_config={"mode": "template", "per_account_daily_action_limit": 2},
            ),
            actor="tester",
        )

    session.rollback()
    session.refresh(task)
    assert task.type_config["business_region"] == original_region


@pytest.mark.no_postgres
def test_search_join_group_schema_accepts_zero_pacing_hourly_override(session: Session) -> None:
    payload = _payload(pacing_config={"mode": "template", "max_actions_per_hour": 0})
    task = create_search_join_group_task(session, 1, payload, actor="tester")

    assert task.type_config["max_actions_per_hour"] == 20
    assert task.pacing_config["max_actions_per_hour"] == 0

    updated = task_service.update_search_join_group_config(
        session,
        1,
        task.id,
        SearchJoinGroupTaskConfigUpdate(
            target_operation_target_id=17,
            search_bots=[{"username": "jisou", "display_name": "极搜"}],
            pacing_config={"mode": "template", "max_actions_per_hour": 0},
        ),
        actor="tester",
    )

    assert updated.pacing_config["max_actions_per_hour"] == 0


@pytest.mark.no_postgres
def test_search_join_group_partial_update_preserves_existing_keyword_material(session: Session) -> None:
    task = create_search_join_group_task(session, 1, _payload(), actor="tester")
    original_hashes = list(task.type_config["keyword_hashes"])
    original_ciphertexts = list(task.type_config["keyword_text_ciphertexts"])

    updated = task_service.update_search_join_group_config(
        session,
        1,
        task.id,
        SearchJoinGroupTaskConfigUpdate(business_region="CN-ZZ"),
        actor="tester",
    )

    assert updated.type_config["business_region"] == "CN-ZZ"
    assert updated.type_config["keyword_hashes"] == original_hashes
    assert updated.type_config["keyword_text_ciphertexts"] == original_ciphertexts


@pytest.mark.no_postgres
def test_search_join_group_update_normalizes_legacy_post_join_navigation(session: Session) -> None:
    task = create_search_join_group_task(session, 1, _payload(), actor="tester")
    task.type_config = {
        **task.type_config,
        "post_join_safe_navigation_min": 1,
        "post_join_safe_navigation_max": 1,
    }
    session.commit()

    updated = task_service.update_search_join_group_config(
        session,
        1,
        task.id,
        SearchJoinGroupTaskConfigUpdate(target_count=8),
        actor="tester",
    )

    assert updated.type_config["post_join_safe_navigation_min"] == 0
    assert updated.type_config["post_join_safe_navigation_max"] == 0

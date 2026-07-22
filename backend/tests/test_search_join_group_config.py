from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import AccountPool, Action, OperationTarget, SearchJoinPacingDecision, Task, Tenant, TgAccount
from app.security import decrypt_secret, encrypt_secret
from app.schemas.task_center import (
    SearchJoinGroupSimpleTaskCreate,
    SearchJoinGroupTaskConfigUpdate,
    SearchJoinGroupTaskCreate,
    SearchRankDeboostSimpleTaskCreate,
    TaskSettingsUpdate,
    TaskUpdate,
)
from app.services.task_center import service as task_service
from app.services.task_center import search_click_target_progress as target_progress
from app.services.task_center.service import (
    create_and_start_search_join_group_task,
    create_search_join_group_task,
    create_simple_search_join_group_task,
    update_task,
    update_task_settings,
)


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Tenant(id=1, name="默认运营空间"))
        db.add_all([
            AccountPool(id=7, tenant_id=1, name="搜索执行组", pool_purpose="normal"),
            AccountPool(id=9, tenant_id=1, name="搜索执行组二", pool_purpose="normal"),
            AccountPool(id=8, tenant_id=1, name="黑搜索执行组", pool_purpose="rank_deboost"),
        ])
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
        db.add_all([
            TgAccount(
                id=account_id,
                tenant_id=1,
                pool_id=7 if account_id <= 108 else 9,
                display_name=f"搜索账号 {account_id}",
                phone_masked=str(account_id),
                status="在线",
                session_ciphertext=f"session-{account_id}",
            )
            for account_id in range(101, 117)
        ])
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


def _simple_payload(**overrides) -> SearchJoinGroupSimpleTaskCreate:
    data = {
        "target_title": "上海留学交流群",
        "target_link": "https://t.me/shanghai_study_group",
        "keywords": ["上海 留学"],
        "daily_target_count": 8,
        "account_group_id": 7,
        "max_actions_per_day": 9,
        "scheduled_end": datetime(2030, 1, 1, tzinfo=timezone.utc),
        "daily_jitter_percent": 20,
        "hourly_jitter_percent": 30,
    }
    data.update(overrides)
    return SearchJoinGroupSimpleTaskCreate(**data)


@pytest.mark.no_postgres
def test_simple_search_join_create_resolves_public_link_without_exposing_target_id(session: Session) -> None:
    task = create_simple_search_join_group_task(
        session,
        1,
        _simple_payload(target_title="上海留学官方交流群"),
        actor="tester",
    )

    assert task.name == "上海留学官方交流群 搜索目标群点击 每日 8 次"
    assert task.type_config["target_operation_target_id"] == 17
    assert task.type_config["target_title"] == "上海留学官方交流群"
    assert task.type_config["target_link"] == "https://t.me/shanghai_study_group"
    assert session.get(OperationTarget, 17).title == "上海留学交流群"


@pytest.mark.no_postgres
def test_simple_search_join_create_creates_missing_public_group_target(session: Session) -> None:
    task = create_simple_search_join_group_task(
        session,
        1,
        _simple_payload(
            target_title="河南郑州学生会",
            target_link="https://t.me/zzxshxc",
            keywords=["河南郑州学生会"],
            daily_target_count=1,
        ),
        actor="tester",
    )

    target = session.query(OperationTarget).filter_by(tenant_id=1, tg_peer_id="zzxshxc").one()
    assert target.target_type == "group"
    assert target.title == "河南郑州学生会"
    assert task.type_config["target_operation_target_id"] == target.id


@pytest.mark.no_postgres
def test_simple_search_join_create_rejects_internal_target_id_input() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        _simple_payload(
            target_operation_target_id=17,
        )


@pytest.mark.no_postgres
def test_simple_search_join_create_uses_system_name_and_policy(session: Session) -> None:
    task = create_simple_search_join_group_task(
        session,
        1,
        _simple_payload(keywords=["上海 留学", "上海 国际学校"]),
        actor="tester",
    )

    assert task.name == "上海留学交流群 搜索目标群点击 每日 8 次"
    assert task.type_config["target_operation_target_id"] == 17
    assert task.type_config["daily_target_count"] == 8
    assert "target_count" not in task.type_config
    assert task.type_config["search_bots"] == [{"username": "jisou", "display_name": "极搜"}]
    assert task.account_config["selection_mode"] == "group"
    assert task.account_config["account_group_id"] == 7
    assert task.pacing_config["max_actions_per_day"] == 9
    assert task.pacing_config["daily_jitter_percent"] == 20
    assert task.pacing_config["hourly_jitter_percent"] == 30
    assert task.scheduled_end == datetime(2030, 1, 1, 8, 0, 0)
    assert task.pacing_config["per_account_daily_action_limit"] == 1
    assert task.pacing_config["skip_probability_per_action"] == 0
    assert task.type_config["strict_daily_target"] is True


@pytest.mark.no_postgres
def test_simple_search_join_persists_independent_click_target_and_repeat_application_mode(session: Session) -> None:
    task = create_simple_search_join_group_task(
        session,
        1,
        _simple_payload(
            daily_click_target_count=500,
            daily_target_count=80,
            allow_same_account_repeat_application=True,
            max_actions_per_day=500,
        ),
        actor="tester",
    )

    assert task.name == "上海留学交流群 搜索目标群点击 每日点击 500 次（加入目标 80 次）"
    assert task.type_config["daily_click_target_count"] == 500
    assert task.type_config["daily_target_count"] == 80
    assert task.type_config["allow_same_account_repeat_application"] is True
    assert task.pacing_config["max_actions_per_day"] == 500


@pytest.mark.no_postgres
def test_simple_search_join_edit_requeues_dual_target_with_repeat_application_mode(session: Session) -> None:
    task = create_simple_search_join_group_task(session, 1, _simple_payload(), actor="tester")
    task.status = "completed"
    task.next_run_at = None
    session.commit()

    updated = task_service.update_search_join_group_config(
        session,
        1,
        task.id,
        SearchJoinGroupTaskConfigUpdate(
            daily_click_target_count=500,
            daily_target_count=80,
            allow_same_account_repeat_application=True,
            actions_per_round=20,
            max_actions_per_hour=500,
            hourly_min_successful_joins=500,
            max_actions_per_day=500,
        ),
        actor="tester",
    )

    assert updated.status == "running"
    assert updated.next_run_at is not None
    assert updated.name == "上海留学交流群 搜索目标群点击 每日点击 500 次（加入目标 80 次）"
    assert updated.type_config["daily_click_target_count"] == 500
    assert updated.type_config["daily_target_count"] == 80
    assert updated.type_config["allow_same_account_repeat_application"] is True
    assert updated.type_config["actions_per_round"] == 20
    assert updated.type_config["max_actions_per_hour"] == 500
    assert updated.type_config["hourly_min_successful_joins"] == 500
    assert updated.pacing_config["max_actions_per_day"] == 500


@pytest.mark.no_postgres
def test_repeat_application_setting_requeues_existing_search_join_task(session: Session) -> None:
    task = create_simple_search_join_group_task(session, 1, _simple_payload(), actor="tester")
    task.status = "running"
    task.next_run_at = None
    session.commit()

    updated = task_service.update_search_join_group_config(
        session,
        1,
        task.id,
        SearchJoinGroupTaskConfigUpdate(allow_same_account_repeat_application=True),
        actor="tester",
    )

    assert updated.type_config["allow_same_account_repeat_application"] is True
    assert updated.next_run_at is not None


@pytest.mark.no_postgres
def test_simple_search_join_rejects_daily_target_without_configured_account_capacity(session: Session) -> None:
    with pytest.raises(ValueError, match="daily_target_capacity_insufficient"):
        create_simple_search_join_group_task(
            session,
            1,
            _simple_payload(daily_target_count=9, max_actions_per_day=9),
            actor="tester",
        )


@pytest.mark.no_postgres
def test_simple_search_join_allows_explicit_daily_account_limit_to_cover_target(session: Session) -> None:
    task = create_simple_search_join_group_task(
        session,
        1,
        _simple_payload(
            daily_target_count=9,
            max_actions_per_day=9,
            per_account_daily_action_limit=2,
        ),
        actor="tester",
    )

    assert task.pacing_config["per_account_daily_action_limit"] == 2


@pytest.mark.no_postgres
def test_simple_search_join_edit_rejects_daily_target_without_configured_account_capacity(session: Session) -> None:
    task = create_simple_search_join_group_task(session, 1, _simple_payload(), actor="tester")

    with pytest.raises(ValueError, match="daily_target_capacity_insufficient"):
        task_service.update_search_join_group_config(
            session,
            1,
            task.id,
            SearchJoinGroupTaskConfigUpdate(daily_target_count=9, max_actions_per_day=9),
            actor="tester",
        )

    assert task.type_config["daily_target_count"] == 8
    assert task.pacing_config["per_account_daily_action_limit"] == 1


@pytest.mark.no_postgres
def test_simple_search_join_create_uses_daily_target_not_lifecycle_target(session: Session) -> None:
    payload = SearchJoinGroupSimpleTaskCreate(
        target_title="上海留学交流群",
        target_link="https://t.me/shanghai_study_group",
        keywords=["上海 留学"],
        daily_target_count=8,
        account_group_id=7,
        max_actions_per_day=8,
        scheduled_end=datetime(2030, 1, 1, tzinfo=timezone.utc),
        daily_jitter_percent=20,
        hourly_jitter_percent=30,
    )

    task = create_simple_search_join_group_task(session, 1, payload, actor="tester")

    assert task.name == "上海留学交流群 搜索目标群点击 每日 8 次"
    assert task.type_config["daily_target_count"] == 8
    assert "target_count" not in task.type_config
    assert task.pacing_config["max_actions_per_day"] == 8


@pytest.mark.no_postgres
def test_simple_search_join_rejects_daily_target_above_daily_action_budget() -> None:
    with pytest.raises(ValidationError, match="max_actions_per_day"):
        SearchJoinGroupSimpleTaskCreate(
            target_title="上海留学交流群",
            target_link="https://t.me/shanghai_study_group",
            keywords=["上海 留学"],
            daily_target_count=9,
            account_group_id=7,
            max_actions_per_day=8,
            scheduled_end=datetime(2030, 1, 1, tzinfo=timezone.utc),
            daily_jitter_percent=20,
            hourly_jitter_percent=30,
        )


@pytest.mark.no_postgres
def test_simple_search_join_edit_regenerates_system_name(session: Session) -> None:
    task = create_simple_search_join_group_task(
        session,
        1,
        _simple_payload(),
        actor="tester",
    )
    task.type_config = {key: value for key, value in task.type_config.items() if key != "strict_daily_target"}
    task.pacing_config = {**task.pacing_config, "skip_probability_per_action": 0.1}
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
        SearchJoinGroupTaskConfigUpdate(
            target_title="北京留学交流群",
            target_link="https://t.me/beijing_study_group",
        ),
        actor="tester",
    )
    assert target_updated.name == "北京留学交流群 搜索目标群点击 每日 8 次"
    assert target_updated.pacing_config["skip_probability_per_action"] == 0.1
    assert "strict_daily_target" not in target_updated.type_config

    updated = task_service.update_search_join_group_config(
        session,
        1,
        task.id,
        SearchJoinGroupTaskConfigUpdate(daily_target_count=7),
        actor="tester",
    )

    assert updated.name == "北京留学交流群 搜索目标群点击 每日 7 次"


@pytest.mark.no_postgres
def test_simple_search_join_edit_updates_operator_execution_controls(session: Session) -> None:
    task = create_simple_search_join_group_task(session, 1, _simple_payload(), actor="tester")
    task.type_config = {key: value for key, value in task.type_config.items() if key != "strict_daily_target"}
    task.pacing_config = {**task.pacing_config, "skip_probability_per_action": 0.1}
    session.add(
        SearchJoinPacingDecision(
            tenant_id=1,
            task_id=task.id,
            decision_scope="action",
            scope_key="2026-07-21:101:keyword:10",
        )
    )
    session.commit()

    updated = task_service.update_search_join_group_config(
        session,
        1,
        task.id,
        SearchJoinGroupTaskConfigUpdate(
            account_group_id=9,
            max_actions_per_day=8,
            per_account_daily_action_limit=2,
            scheduled_end=datetime(2030, 2, 1, tzinfo=timezone.utc),
            daily_jitter_percent=10,
            hourly_jitter_percent=15,
            quiet_hours={"start": "23:00", "end": "07:00"},
            enable_strict_daily_target=True,
        ),
        actor="tester",
    )

    assert updated.account_config["selection_mode"] == "group"
    assert updated.account_config["account_group_id"] == 9
    assert updated.pacing_config["max_actions_per_day"] == 8
    assert updated.pacing_config["per_account_daily_action_limit"] == 2
    assert updated.pacing_config["daily_jitter_percent"] == 10
    assert updated.pacing_config["hourly_jitter_percent"] == 15
    assert updated.pacing_config["quiet_hours"] == {"start": "23:00", "end": "07:00", "timezone": "Asia/Shanghai"}
    assert updated.pacing_config["skip_probability_per_action"] == 0
    assert updated.type_config["strict_daily_target"] is True
    assert updated.scheduled_end == datetime(2030, 2, 1, 8, 0, 0)
    assert session.query(SearchJoinPacingDecision).filter_by(task_id=task.id).count() == 0


@pytest.mark.no_postgres
def test_advanced_daily_search_join_edit_preserves_behavior_pacing(session: Session) -> None:
    task = create_search_join_group_task(
        session,
        1,
        _payload(daily_target_count=8),
        actor="tester",
    )

    updated = task_service.update_search_join_group_config(
        session,
        1,
        task.id,
        SearchJoinGroupTaskConfigUpdate(max_actions_per_day=9),
        actor="tester",
    )

    assert updated.pacing_config["skip_probability_per_action"] == 0.1
    assert "strict_daily_target" not in updated.type_config


@pytest.mark.no_postgres
def test_search_join_group_rejects_strict_daily_target_for_lifecycle_task(session: Session) -> None:
    task = create_search_join_group_task(session, 1, _payload(target_count=8), actor="tester")

    with pytest.raises(ValueError, match="严格每日目标仅适用于 daily_target_count 任务"):
        task_service.update_search_join_group_config(
            session,
            1,
            task.id,
            SearchJoinGroupTaskConfigUpdate(enable_strict_daily_target=True),
            actor="tester",
        )


@pytest.mark.no_postgres
def test_search_join_daily_target_patch_replaces_legacy_lifecycle_target(session: Session) -> None:
    task = create_search_join_group_task(session, 1, _payload(target_count=12), actor="tester")

    updated = task_service.update_search_join_group_config(
        session,
        1,
        task.id,
        SearchJoinGroupTaskConfigUpdate(daily_target_count=8, max_actions_per_day=8),
        actor="tester",
    )

    assert updated.name == "上海留学交流群 搜索目标群点击 每日 8 次"
    assert updated.type_config["daily_target_count"] == 8
    assert "target_count" not in updated.type_config
    assert updated.pacing_config["max_actions_per_day"] == 8


@pytest.mark.no_postgres
def test_search_join_daily_target_patch_reopens_completed_legacy_task(session: Session) -> None:
    task = create_search_join_group_task(session, 1, _payload(target_count=1), actor="tester")
    task.status = "completed"
    task.next_run_at = None
    task.stats = {"completion_reason": "target_count_reached"}
    session.commit()

    updated = task_service.update_search_join_group_config(
        session,
        1,
        task.id,
        SearchJoinGroupTaskConfigUpdate(daily_target_count=8, max_actions_per_day=8),
        actor="tester",
    )

    assert updated.status == "running"
    assert updated.next_run_at is not None
    assert "completion_reason" not in updated.stats
    assert updated.stats["search_click_target"]["scope"] == "daily"


@pytest.mark.no_postgres
def test_deadline_extension_reopens_incomplete_daily_search_join_task(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    now_value = datetime(2030, 1, 1, 10, 0, 0)
    monkeypatch.setattr(task_service, "_now", lambda: now_value)
    task = create_simple_search_join_group_task(
        session,
        1,
        _simple_payload(scheduled_end=datetime(2030, 1, 2, tzinfo=timezone.utc)),
        actor="tester",
    )
    task.status = "completed"
    task.next_run_at = None
    task.scheduled_end = datetime(2030, 1, 1, 9, 0, 0)
    session.commit()

    updated = task_service.update_search_join_group_config(
        session,
        1,
        task.id,
        SearchJoinGroupTaskConfigUpdate(scheduled_end=datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)),
        actor="tester",
    )

    assert updated.status == "running"
    assert updated.next_run_at == now_value
    assert updated.scheduled_end == datetime(2030, 1, 1, 20, 0, 0)


@pytest.mark.no_postgres
def test_deadline_extension_keeps_daily_task_ready_for_the_next_day(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    now_value = datetime(2030, 1, 1, 10, 0, 0)
    monkeypatch.setattr(task_service, "_now", lambda: now_value)
    monkeypatch.setattr(target_progress, "beijing_now", lambda: now_value)
    task = create_simple_search_join_group_task(
        session,
        1,
        _simple_payload(daily_target_count=1, max_actions_per_day=1, scheduled_end=datetime(2030, 1, 2, tzinfo=timezone.utc)),
        actor="tester",
    )
    task.status = "completed"
    task.next_run_at = None
    task.scheduled_end = datetime(2030, 1, 1, 9, 0, 0)
    session.add(
        Action(
            tenant_id=1,
            task_id=task.id,
            task_type="search_join_group",
            action_type="search_join",
            status="success",
            executed_at=now_value,
            result={"join_status": "membership_observed"},
        )
    )
    session.commit()

    updated = task_service.update_search_join_group_config(
        session,
        1,
        task.id,
        SearchJoinGroupTaskConfigUpdate(scheduled_end=datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)),
        actor="tester",
    )

    assert updated.status == "running"
    assert updated.next_run_at == now_value


@pytest.mark.no_postgres
def test_simple_search_click_rejects_expired_or_cleared_deadline(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    now_value = datetime(2030, 1, 1)
    monkeypatch.setattr(task_service, "_now", lambda: now_value)
    with pytest.raises(ValueError, match="完成截止时间必须晚于当前时间"):
        create_simple_search_join_group_task(session, 1, _simple_payload(scheduled_end=now_value), actor="tester")

    task = create_simple_search_join_group_task(
        session,
        1,
        _simple_payload(scheduled_end=datetime(2030, 1, 2, tzinfo=timezone.utc)),
        actor="tester",
    )
    with pytest.raises(ValueError, match="完成截止时间必须晚于当前时间"):
        task_service.update_search_join_group_config(
            session,
            1,
            task.id,
            SearchJoinGroupTaskConfigUpdate(scheduled_end=None),
            actor="tester",
        )


@pytest.mark.no_postgres
def test_simple_search_join_create_rejects_system_managed_fields() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        _simple_payload(search_bots=[{"username": "other"}])


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
        ("retry_policy", {"max_retries": 3}),
        ("risk_config", {"mode": "caller_selected"}),
    ],
)
def test_simple_search_click_create_rejects_all_system_managed_inputs(payload_type, field: str, value: object) -> None:
    target = {"daily_target_count": 8} if payload_type is SearchJoinGroupSimpleTaskCreate else {"target_count": 12}
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        payload_type(
            target_title="上海留学交流群",
            target_link="https://t.me/shanghai_study_group",
            keywords=["上海 留学"],
            account_group_id=7,
            max_actions_per_day=9,
            scheduled_end=datetime(2030, 1, 1, tzinfo=timezone.utc),
            daily_jitter_percent=20,
            hourly_jitter_percent=30,
            **target,
            **{field: value},
        )


@pytest.mark.no_postgres
def test_simple_search_rank_deboost_rejects_search_join_daily_account_limit() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        SearchRankDeboostSimpleTaskCreate(
            target_title="上海留学交流群",
            target_link="https://t.me/shanghai_study_group",
            keywords=["上海 留学"],
            target_count=12,
            account_group_id=8,
            max_actions_per_day=9,
            scheduled_end=datetime(2030, 1, 1, tzinfo=timezone.utc),
            daily_jitter_percent=20,
            hourly_jitter_percent=30,
            per_account_daily_action_limit=2,
        )


@pytest.mark.no_postgres
def test_simple_search_join_create_deduplicates_normalized_keywords() -> None:
    payload = _simple_payload(keywords=["  Study   Group  ", "study group"])

    assert payload.keywords == ["Study   Group"]


@pytest.mark.no_postgres
def test_simple_search_join_create_does_not_impose_an_unstated_daily_target_cap() -> None:
    payload = _simple_payload(daily_target_count=100_001, max_actions_per_day=100_001)

    assert payload.daily_target_count == 100_001


@pytest.mark.no_postgres
@pytest.mark.parametrize("target_link", ["https://t.me/+invite", "https://t.me/joinchat/invite", "@shanghai_study_group"])
def test_simple_search_join_create_rejects_target_without_public_link(session: Session, target_link: str) -> None:
    with pytest.raises(ValueError, match="公开 Telegram 链接"):
        create_simple_search_join_group_task(
            session,
            1,
            _simple_payload(target_link=target_link),
            actor="tester",
        )


@pytest.mark.no_postgres
def test_simple_search_join_create_rejects_rank_deboost_account_group(session: Session) -> None:
    with pytest.raises(ValueError, match="普通搜索点击任务只能使用普通账号组"):
        create_simple_search_join_group_task(
            session,
            1,
            _simple_payload(account_group_id=8),
            actor="tester",
        )


@pytest.mark.no_postgres
def test_simple_search_join_create_rejects_account_group_with_conflicting_system_key(session: Session) -> None:
    session.add(
        AccountPool(
            id=10,
            tenant_id=1,
            name="冲突普通搜索执行组",
            pool_purpose="normal",
            system_key="rank_deboost",
        )
    )
    session.commit()

    with pytest.raises(ValueError, match="普通搜索点击任务只能使用普通账号组"):
        create_simple_search_join_group_task(
            session,
            1,
            _simple_payload(account_group_id=10),
            actor="tester",
        )


@pytest.mark.no_postgres
def test_simple_search_click_rejects_invalid_quiet_hours() -> None:
    with pytest.raises(ValidationError, match="quiet_hours.start 必须是 HH:MM"):
        _simple_payload(quiet_hours={"start": "25:00", "end": "08:00"})


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
def test_generic_search_click_updates_cannot_bypass_dedicated_contract(session: Session) -> None:
    task = create_simple_search_join_group_task(session, 1, _simple_payload(daily_target_count=1), actor="tester")
    original_account_config = dict(task.account_config)
    original_deadline = task.scheduled_end

    with pytest.raises(ValueError, match="专用编辑接口"):
        update_task(
            session,
            1,
            task.id,
            TaskUpdate(account_config={"selection_mode": "manual", "account_ids": [101]}),
            actor="tester",
        )
    with pytest.raises(ValueError, match="专用编辑接口"):
        update_task_settings(
            session,
            1,
            task.id,
            TaskSettingsUpdate(scheduled_end=None),
            actor="tester",
        )

    assert task.account_config == original_account_config
    assert task.scheduled_end == original_deadline

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
def test_search_join_group_config_update_rejects_system_managed_fields_without_mutation(session: Session) -> None:
    task = create_search_join_group_task(session, 1, _payload(), actor="tester")
    original_region = task.type_config["business_region"]

    with pytest.raises(ValueError, match="系统托管字段"):
        task_service.update_search_join_group_config(
            session,
            1,
            task.id,
            SearchJoinGroupTaskConfigUpdate(
                search_bots=[{"username": "jisou", "display_name": "极搜"}],
                business_region="CN-ZZ",
            ),
            actor="tester",
        )

    assert task.type_config["business_region"] == original_region


@pytest.mark.no_postgres
def test_search_join_pacing_update_only_supersedes_pre_gateway_executing_action(session: Session) -> None:
    task = create_simple_search_join_group_task(session, 1, _simple_payload(daily_target_count=1), actor="tester")
    pre_gateway = Action(
        tenant_id=1,
        task_id=task.id,
        task_type=task.type,
        action_type="search_join",
        account_id=101,
        status="executing",
        payload={},
        result={"gateway_call_state": "before_call"},
    )
    gateway_started = Action(
        tenant_id=1,
        task_id=task.id,
        task_type=task.type,
        action_type="search_join",
        account_id=None,
        status="executing",
        payload={},
        result={"gateway_call_state": "started"},
    )
    session.add_all([pre_gateway, gateway_started])
    session.commit()

    task_service.update_search_join_group_config(
        session,
        1,
        task.id,
        SearchJoinGroupTaskConfigUpdate(max_actions_per_day=1),
        actor="tester",
    )

    assert pre_gateway.status == "skipped"
    assert pre_gateway.result["error_code"] == "plan_superseded"
    assert gateway_started.status == "executing"


@pytest.mark.no_postgres
def test_search_join_pacing_update_supersedes_unmarked_executing_action(session: Session) -> None:
    task = create_simple_search_join_group_task(session, 1, _simple_payload(daily_target_count=1), actor="tester")
    action = Action(
        tenant_id=1,
        task_id=task.id,
        task_type=task.type,
        action_type="search_join",
        account_id=101,
        status="executing",
        payload={},
        result={},
    )
    session.add(action)
    session.commit()

    task_service.update_search_join_group_config(
        session,
        1,
        task.id,
        SearchJoinGroupTaskConfigUpdate(max_actions_per_day=1),
        actor="tester",
    )

    assert action.status == "skipped"
    assert action.result["error_code"] == "plan_superseded"


@pytest.mark.no_postgres
def test_search_join_group_operator_patch_rejects_raw_pacing_override(session: Session) -> None:
    payload = _payload(pacing_config={"mode": "template", "max_actions_per_hour": 0})
    task = create_search_join_group_task(session, 1, payload, actor="tester")

    assert task.type_config["max_actions_per_hour"] == 20
    assert task.pacing_config["max_actions_per_hour"] == 0

    with pytest.raises(ValueError, match="系统托管字段"):
        task_service.update_search_join_group_config(
            session,
            1,
            task.id,
            SearchJoinGroupTaskConfigUpdate(pacing_config={"mode": "template", "max_actions_per_hour": 0}),
            actor="tester",
        )


@pytest.mark.no_postgres
def test_search_join_group_partial_update_preserves_existing_keyword_material(session: Session) -> None:
    task = create_search_join_group_task(session, 1, _payload(), actor="tester")
    original_hashes = list(task.type_config["keyword_hashes"])
    original_ciphertexts = list(task.type_config["keyword_text_ciphertexts"])

    updated = task_service.update_search_join_group_config(
        session,
        1,
        task.id,
        SearchJoinGroupTaskConfigUpdate(target_count=8),
        actor="tester",
    )

    assert updated.type_config["target_count"] == 8
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

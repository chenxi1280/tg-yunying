from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import OperationTarget, RuleSet, RuleSetVersion, Tenant
from app.schemas.task_center import GroupAIChatTaskCreate
from app.services.task_center.config_normalization import normalize_operation_target_references
from app.services.task_center.service import create_group_ai_chat_task


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load_required_rule_binding_migration():
    migration_path = PROJECT_ROOT / "backend/migrations/versions/0072_backfill_required_task_rule_binding.py"
    spec = importlib.util.spec_from_file_location("migration_0072_required_rule_binding", migration_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.no_postgres
def test_group_ai_config_accepts_topic_teacher_and_consecutive_settings() -> None:
    payload = GroupAIChatTaskCreate(
        name="AI 活群",
        target_group_id=7,
        topic_directions=[
            {"title": "升学规划", "description": "围绕择校节奏聊", "weight": 2},
            {"title": "材料准备", "description": "围绕材料清单聊", "weight": 1},
        ],
        teacher_targets=[
            {"name": "王老师", "description": "负责报名答疑", "priority": 20},
            {"name": "李老师", "description": "负责材料审核", "priority": 10},
        ],
        consecutive_message_enabled=True,
        consecutive_message_min=2,
        consecutive_message_max=4,
        consecutive_message_probability=0.3,
        hourly_min_messages=10,
    )

    data = payload.model_dump(mode="json")

    assert data["topic_directions"][0]["title"] == "升学规划"
    assert data["teacher_targets"][0]["name"] == "王老师"
    assert data["consecutive_message_min"] == 2


@pytest.mark.no_postgres
def test_group_ai_task_creation_binds_default_rule_set() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.commit()

        task = create_group_ai_chat_task(
            session,
            1,
            GroupAIChatTaskCreate(name="AI 活群", target_group_id=7, hourly_min_messages=10),
            actor="tester",
        )
        rule_set = session.get(RuleSet, task.type_config["rule_set_id"])

    assert rule_set is not None
    assert rule_set.name == "默认运营规则集"
    assert "rule_set_version_id" not in task.type_config


@pytest.mark.no_postgres
def test_group_ai_all_account_task_defaults_to_daily_coverage_on_create() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.commit()

        task = create_group_ai_chat_task(
            session,
            1,
            GroupAIChatTaskCreate(name="AI 活群", target_group_id=7, hourly_min_messages=10),
            actor="tester",
        )

    assert task.account_config["selection_mode"] == "all"
    assert task.type_config["account_coverage_mode"] == "all_accounts_daily"


@pytest.mark.no_postgres
def test_default_rule_binding_repairs_draft_active_version() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        rule_set = RuleSet(
            id=99,
            tenant_id=1,
            name="默认运营规则集",
            status="active",
            task_types=["group_ai_chat"],
            active_version_id=100,
        )
        session.add(rule_set)
        session.add(RuleSetVersion(id=100, tenant_id=1, rule_set_id=99, version=1, status="draft"))
        session.commit()

        task = create_group_ai_chat_task(
            session,
            1,
            GroupAIChatTaskCreate(name="AI 活群", target_group_id=7, hourly_min_messages=10),
            actor="tester",
        )
        session.refresh(rule_set)
        active = session.get(RuleSetVersion, rule_set.active_version_id)

    assert task.type_config["rule_set_id"] == 99
    assert active.status == "published"
    assert active.version == 2


@pytest.mark.no_postgres
def test_required_rule_binding_migration_marks_running_task_for_retry() -> None:
    migration = _load_required_rule_binding_migration()
    current_time = datetime(2026, 7, 1, tzinfo=timezone.utc)

    values = migration._task_update_values(
        {"target_group_id": 7},
        {
            "hard_hourly_last_blockers": {"rule_binding_missing": 10},
            "hard_hourly_next_check_at": "2026-07-01T09:43:13",
        },
        status="running",
        last_error="任务必须绑定已发布规则集版本",
        rule_set_id=123,
        current_time=current_time,
    )

    assert values["type_config"]["rule_set_id"] == 123
    assert values["last_error"] == ""
    assert values["next_run_at"] == current_time
    assert "hard_hourly_last_blockers" not in values["stats"]
    assert "hard_hourly_next_check_at" not in values["stats"]


@pytest.mark.no_postgres
def test_group_ai_config_rejects_legacy_topic_hint_input() -> None:
    with pytest.raises(ValidationError, match="topic_hint"):
        GroupAIChatTaskCreate(
            name="AI 活群",
            target_group_id=7,
            topic_hint="旧话题",
            hourly_min_messages=10,
        )


@pytest.mark.no_postgres
def test_group_ai_config_accepts_plain_line_topic_and_chat_targets() -> None:
    payload = GroupAIChatTaskCreate(
        name="AI 活群",
        target_group_id=7,
        topic_directions="郑州楼凤妹子怎么样\n主任最近约新妹子了\n精品榜的妹子真好",
        teacher_targets="花花老师身材服务真好\n新人榜单妹子",
        hourly_min_messages=10,
    )

    data = payload.model_dump(mode="json")

    assert [item["title"] for item in data["topic_directions"]] == [
        "郑州楼凤妹子怎么样",
        "主任最近约新妹子了",
        "精品榜的妹子真好",
    ]
    assert [item["weight"] for item in data["topic_directions"]] == [3.0, 2.0, 1.0]
    assert [item["name"] for item in data["teacher_targets"]] == ["花花老师身材服务真好", "新人榜单妹子"]
    assert [item["priority"] for item in data["teacher_targets"]] == [2, 1]


@pytest.mark.no_postgres
def test_topic_hint_migration_moves_legacy_hint_to_topic_directions() -> None:
    migration = PROJECT_ROOT / "backend/migrations/versions/0070_migrate_group_ai_topic_hint.py"
    source = migration.read_text()

    assert "topic_hint" in source
    assert "topic_directions" in source
    assert "jsonb_set" in source
    assert "- 'topic_hint'" in source


@pytest.mark.no_postgres
def test_group_ai_config_accepts_all_accounts_daily_coverage_settings() -> None:
    payload = GroupAIChatTaskCreate(
        name="AI 活群",
        target_group_id=7,
        account_coverage_mode="all_accounts_daily",
        per_account_daily_min_messages=1,
        per_account_daily_max_messages=2,
        coverage_window_hours=24,
        hourly_min_messages=10,
    )

    data = payload.model_dump(mode="json")

    assert data["account_coverage_mode"] == "all_accounts_daily"
    assert data["per_account_daily_min_messages"] == 1
    assert data["per_account_daily_max_messages"] == 2
    assert data["coverage_window_hours"] == 24


@pytest.mark.no_postgres
def test_group_ai_config_rejects_invalid_all_accounts_daily_coverage_settings() -> None:
    with pytest.raises(ValidationError):
        GroupAIChatTaskCreate(
            name="AI 活群",
            target_group_id=7,
            account_coverage_mode="all_accounts_daily",
            per_account_daily_min_messages=2,
            per_account_daily_max_messages=1,
            coverage_window_hours=24,
            hourly_min_messages=10,
        )

    with pytest.raises(ValidationError):
        GroupAIChatTaskCreate(
            name="AI 活群",
            target_group_id=7,
            account_coverage_mode="all_accounts_daily",
            per_account_daily_min_messages=1,
            per_account_daily_max_messages=2,
            coverage_window_hours=12,
            hourly_min_messages=10,
        )


@pytest.mark.no_postgres
def test_group_ai_config_rejects_invalid_topic_teacher_and_consecutive_settings() -> None:
    with pytest.raises(ValidationError):
        GroupAIChatTaskCreate(
            name="AI 活群",
            target_group_id=7,
            topic_directions=[{"title": "", "weight": 1}],
            teacher_targets=[{"name": "", "priority": 1}],
            consecutive_message_enabled=True,
            consecutive_message_min=4,
            consecutive_message_max=2,
            hourly_min_messages=10,
        )


def test_group_ai_config_prefers_stable_duplicate_target() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(
            OperationTarget(
                id=2149,
                tenant_id=1,
                target_type="group",
                tg_peer_id=" @qdsfxy",
                title="青岛师范学院",
                username="",
                can_send=True,
                auth_status="已授权运营",
            )
        )
        session.add(
            OperationTarget(
                id=2761,
                tenant_id=1,
                target_type="group",
                tg_peer_id="-1003426646531",
                title="青岛师范学院",
                username="qdsfxy",
                can_send=True,
                auth_status="已授权运营",
            )
        )
        session.commit()

        normalized = normalize_operation_target_references(
            session,
            1,
            "group_ai_chat",
            {"target_operation_target_id": 2149},
        )

    assert normalized["target_operation_target_id"] == 2761
    assert normalized["target_group_id"] > 0
    assert normalized["target_group_name"] == "青岛师范学院"


def test_group_ai_config_prefers_stable_duplicate_target_by_username() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(
            OperationTarget(
                id=485,
                tenant_id=1,
                target_type="group",
                tg_peer_id="-1003583171851",
                title="天津",
                username="zzjinli",
                can_send=True,
                auth_status="已授权运营",
            )
        )
        session.add(
            OperationTarget(
                id=1251,
                tenant_id=1,
                target_type="group",
                tg_peer_id="zzjinli",
                title="zzjinli",
                username="zzjinli",
                can_send=True,
                auth_status="已授权运营",
            )
        )
        session.commit()

        normalized = normalize_operation_target_references(
            session,
            1,
            "group_ai_chat",
            {"target_operation_target_id": 1251},
        )

    assert normalized["target_operation_target_id"] == 485
    assert normalized["target_group_name"] == "天津"

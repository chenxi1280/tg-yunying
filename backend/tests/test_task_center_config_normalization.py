from __future__ import annotations

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import OperationTarget, Tenant
from app.schemas.task_center import GroupAIChatTaskCreate
from app.services.task_center.config_normalization import normalize_operation_target_references


@pytest.mark.no_postgres
def test_group_ai_config_accepts_topic_teacher_and_consecutive_settings() -> None:
    payload = GroupAIChatTaskCreate(
        name="AI 活群",
        target_group_id=7,
        topic_hint="升学规划",
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

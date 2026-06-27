from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Task, Tenant
from app.services.telegram_bot_settings import apply_group_ai_settings_from_bot, handle_group_ai_bot_update


@pytest.mark.no_postgres
def test_telegram_bot_group_ai_settings_rejects_non_admin_chat() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间", admin_chat_id="admin-chat", telegram_bot_token_ciphertext="encrypted"))
        session.commit()

        with pytest.raises(PermissionError):
            apply_group_ai_settings_from_bot(
                session,
                tenant_id=1,
                chat_id="other-chat",
                task_id="task-ai",
                payload={"topic_directions": [{"title": "升学规划", "weight": 1}]},
            )


@pytest.mark.no_postgres
def test_telegram_bot_group_ai_settings_updates_task_with_shared_validation() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间", admin_chat_id="admin-chat", telegram_bot_token_ciphertext="encrypted"))
        session.add(
            Task(
                id="task-ai",
                tenant_id=1,
                name="AI 活群",
                type="group_ai_chat",
                status="running",
                type_config={
                    "target_group_id": 7,
                    "topic_hint": "旧话题",
                    "messages_per_round": 4,
                    "reply_min_per_round": 0,
                    "hard_hourly_target_enabled": True,
                    "hourly_min_messages": 60,
                },
            )
        )
        session.commit()

        task = apply_group_ai_settings_from_bot(
            session,
            tenant_id=1,
            chat_id="admin-chat",
            task_id="task-ai",
            payload={
                "topic_directions": [{"title": "升学规划", "description": "择校节奏", "weight": 1}],
                "teacher_targets": [{"name": "王老师", "description": "报名答疑", "priority": 10}],
                "consecutive_message_enabled": True,
                "consecutive_message_min": 2,
                "consecutive_message_max": 3,
                "consecutive_message_probability": 0.5,
            },
        )

    assert task.type_config["topic_directions"][0]["title"] == "升学规划"
    assert task.type_config["teacher_targets"][0]["name"] == "王老师"
    assert task.type_config["consecutive_message_max"] == 3


@pytest.mark.no_postgres
def test_telegram_bot_update_command_saves_group_ai_settings() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间", admin_chat_id="1001", telegram_bot_token_ciphertext="encrypted"))
        session.add(
            Task(
                id="task-ai",
                tenant_id=1,
                name="AI 活群",
                type="group_ai_chat",
                status="running",
                type_config={
                    "target_group_id": 7,
                    "messages_per_round": 4,
                    "reply_min_per_round": 0,
                    "hard_hourly_target_enabled": True,
                    "hourly_min_messages": 60,
                },
            )
        )
        session.commit()

        result = handle_group_ai_bot_update(
            session,
            tenant_id=1,
            update={
                "message": {
                    "chat": {"id": "1001"},
                    "text": '/ai_group_set task-ai {"teacher_targets":[{"name":"王老师","priority":10}]}',
                }
            },
        )

        task = session.get(Task, "task-ai")

    assert result["method"] == "sendMessage"
    assert "已保存" in result["text"]
    assert task.type_config["teacher_targets"][0]["name"] == "王老师"

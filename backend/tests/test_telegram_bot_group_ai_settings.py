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
        session.add(Tenant(id=1, name="默认运营空间", admin_chat_id="admin-chat", telegram_bot_token_ciphertext="encrypted", ai_group_bot_enabled=True))
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
def test_telegram_bot_group_ai_settings_allows_any_configured_admin_chat() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间", admin_chat_id="admin-one, admin-two\nadmin-three", telegram_bot_token_ciphertext="encrypted", ai_group_bot_enabled=True))
        session.add(
            Task(
                id="task-ai",
                tenant_id=1,
                name="AI 活群",
                type="group_ai_chat",
                status="running",
                type_config={"target_group_id": 7, "messages_per_round": 4, "reply_min_per_round": 0},
            )
        )
        session.commit()

        task = apply_group_ai_settings_from_bot(
            session,
            tenant_id=1,
            chat_id="admin-two",
            task_id="task-ai",
            payload={"topic_directions": [{"title": "升学规划", "weight": 1}]},
        )

    assert task.type_config["topic_directions"][0]["title"] == "升学规划"


@pytest.mark.no_postgres
def test_telegram_bot_group_ai_settings_updates_task_with_shared_validation() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间", admin_chat_id="admin-chat", telegram_bot_token_ciphertext="encrypted", ai_group_bot_enabled=True))
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
def test_telegram_bot_group_ai_settings_updates_account_coverage_fields() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间", admin_chat_id="admin-chat", telegram_bot_token_ciphertext="encrypted", ai_group_bot_enabled=True))
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

        task = apply_group_ai_settings_from_bot(
            session,
            tenant_id=1,
            chat_id="admin-chat",
            task_id="task-ai",
            payload={
                "account_coverage_mode": "all_accounts_daily",
                "per_account_daily_min_messages": 1,
                "per_account_daily_max_messages": 2,
                "coverage_window_hours": 24,
            },
        )

    assert task.type_config["account_coverage_mode"] == "all_accounts_daily"
    assert task.type_config["per_account_daily_min_messages"] == 1
    assert task.type_config["per_account_daily_max_messages"] == 2
    assert task.type_config["coverage_window_hours"] == 24


@pytest.mark.no_postgres
def test_telegram_bot_group_ai_settings_requires_ai_group_bot_enabled() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(
            Tenant(
                id=1,
                name="默认运营空间",
                admin_chat_id="admin-chat",
                telegram_bot_token_ciphertext="encrypted",
                ai_group_bot_enabled=False,
            )
        )
        session.add(Task(id="task-ai", tenant_id=1, name="AI 活群", type="group_ai_chat", status="running", type_config={"target_group_id": 7}))
        session.commit()

        with pytest.raises(PermissionError, match="AI 活群 Bot 设置未启用"):
            apply_group_ai_settings_from_bot(
                session,
                tenant_id=1,
                chat_id="admin-chat",
                task_id="task-ai",
                payload={"teacher_targets": [{"name": "王老师", "priority": 10}]},
            )


@pytest.mark.no_postgres
def test_telegram_bot_update_command_saves_group_ai_settings() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间", admin_chat_id="1001", telegram_bot_token_ciphertext="encrypted", ai_group_bot_enabled=True))
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


@pytest.mark.no_postgres
def test_telegram_bot_start_and_admin_reply_when_ai_group_disabled() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(
            Tenant(
                id=1,
                name="默认运营空间",
                admin_chat_id="1001",
                telegram_bot_token_ciphertext="encrypted",
                telegram_bot_webhook_status="registered",
                ai_group_bot_enabled=False,
            )
        )
        session.commit()

        start = handle_group_ai_bot_update(session, tenant_id=1, update={"message": {"chat": {"id": "1001"}, "text": "/start"}})
        admin = handle_group_ai_bot_update(session, tenant_id=1, update={"message": {"chat": {"id": "1001"}, "text": "/admin"}})
        ai_group = handle_group_ai_bot_update(session, tenant_id=1, update={"message": {"chat": {"id": "1001"}, "text": "/ai_group"}})

    assert start["method"] == "sendMessage"
    assert "Bot 已连接" in start["text"]
    assert "AI 活群 Bot 设置未启用" in start["text"]
    assert "Webhook：registered" in admin["text"]
    assert "AI 活群 Bot 设置未启用" in ai_group["text"]


@pytest.mark.no_postgres
def test_telegram_bot_unauthorized_chat_gets_visible_rejection() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(
            Tenant(
                id=1,
                name="默认运营空间",
                admin_chat_id="1001,1002",
                telegram_bot_token_ciphertext="encrypted",
                ai_group_bot_enabled=True,
            )
        )
        session.commit()

        result = handle_group_ai_bot_update(session, tenant_id=1, update={"message": {"chat": {"id": "2001"}, "text": "/admin"}})

    assert result["method"] == "sendMessage"
    assert result["chat_id"] == "2001"
    assert "当前聊天未授权" in result["text"]


@pytest.mark.no_postgres
def test_telegram_bot_tasks_command_returns_inline_keyboard() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间", admin_chat_id="1001", telegram_bot_token_ciphertext="encrypted", ai_group_bot_enabled=True))
        session.add(Task(id="task-ai", tenant_id=1, name="AI 活群", type="group_ai_chat", status="running", type_config={"target_group_id": 7}))
        session.commit()

        result = handle_group_ai_bot_update(
            session,
            tenant_id=1,
            update={"message": {"chat": {"id": "1001"}, "text": "/ai_group_tasks"}},
        )

    keyboard = result["reply_markup"]["inline_keyboard"]
    assert keyboard[0][0]["text"] == "AI 活群"
    assert keyboard[0][0]["callback_data"] == "ai_group:task:task-ai"


@pytest.mark.no_postgres
def test_telegram_bot_callback_selects_task_settings() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间", admin_chat_id="1001", telegram_bot_token_ciphertext="encrypted", ai_group_bot_enabled=True))
        session.add(
            Task(
                id="task-ai",
                tenant_id=1,
                name="AI 活群",
                type="group_ai_chat",
                status="running",
                type_config={
                    "target_group_id": 7,
                    "topic_directions": [{"title": "升学规划", "weight": 1}],
                    "teacher_targets": [{"name": "王老师", "priority": 10}],
                    "consecutive_message_enabled": True,
                    "consecutive_message_min": 2,
                    "consecutive_message_max": 3,
                },
            )
        )
        session.commit()

        result = handle_group_ai_bot_update(
            session,
            tenant_id=1,
            update={"callback_query": {"message": {"chat": {"id": "1001"}}, "data": "ai_group:task:task-ai"}},
        )

    assert result["method"] == "sendMessage"
    assert "话题数：1" in result["text"]
    assert "老师数：1" in result["text"]
    assert result["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "ai_group:edit_topics:task-ai"


@pytest.mark.no_postgres
def test_telegram_bot_button_flow_edits_topics_with_confirm() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间", admin_chat_id="1001", telegram_bot_token_ciphertext="encrypted", ai_group_bot_enabled=True))
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

        start = handle_group_ai_bot_update(
            session,
            tenant_id=1,
            update={"callback_query": {"message": {"chat": {"id": "1001"}}, "data": "ai_group:edit_topics:task-ai"}},
        )
        draft = handle_group_ai_bot_update(
            session,
            tenant_id=1,
            update={"message": {"chat": {"id": "1001"}, "text": "升学规划|择校节奏|2\n报名答疑|1"}},
        )
        saved = handle_group_ai_bot_update(
            session,
            tenant_id=1,
            update={"callback_query": {"message": {"chat": {"id": "1001"}}, "data": "ai_group:confirm:task-ai"}},
        )
        task = session.get(Task, "task-ai")

    assert "话题方向" in start["text"]
    assert draft["reply_markup"]["inline_keyboard"][0][0]["text"] == "确认保存"
    assert "已保存" in saved["text"]
    assert task.type_config["topic_directions"][0]["title"] == "升学规划"
    assert task.type_config["topic_directions"][1]["weight"] == 1


@pytest.mark.no_postgres
def test_telegram_bot_button_flow_edits_account_coverage_with_confirm() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间", admin_chat_id="1001", telegram_bot_token_ciphertext="encrypted", ai_group_bot_enabled=True))
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

        start = handle_group_ai_bot_update(
            session,
            tenant_id=1,
            update={"callback_query": {"message": {"chat": {"id": "1001"}}, "data": "ai_group:edit_coverage:task-ai"}},
        )
        draft = handle_group_ai_bot_update(
            session,
            tenant_id=1,
            update={"message": {"chat": {"id": "1001"}, "text": "开启 1 2"}},
        )
        saved = handle_group_ai_bot_update(
            session,
            tenant_id=1,
            update={"callback_query": {"message": {"chat": {"id": "1001"}}, "data": "ai_group:confirm:task-ai"}},
        )
        task = session.get(Task, "task-ai")

    assert "全账号日覆盖" in start["text"]
    assert draft["reply_markup"]["inline_keyboard"][0][0]["text"] == "确认保存"
    assert "已保存" in saved["text"]
    assert task.type_config["account_coverage_mode"] == "all_accounts_daily"
    assert task.type_config["per_account_daily_max_messages"] == 2

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Task, TelegramBotConversation, Tenant
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
            payload={"topic_directions": "升学规划\n材料准备"},
        )

    assert [item["title"] for item in task.type_config["topic_directions"]] == ["升学规划", "材料准备"]
    assert [item["weight"] for item in task.type_config["topic_directions"]] == [2.0, 1.0]


@pytest.mark.no_postgres
def test_telegram_bot_group_ai_settings_reuses_backend_validation() -> None:
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

        with pytest.raises(ValueError):
            apply_group_ai_settings_from_bot(
                session,
                tenant_id=1,
                chat_id="admin-chat",
                task_id="task-ai",
                payload={"topic_directions": [{"title": "", "weight": 1}]},
            )

        task = session.get(Task, "task-ai")

    assert task.type_config["topic_hint"] == "旧话题"
    assert "topic_directions" not in task.type_config
    assert "teacher_targets" not in task.type_config


@pytest.mark.no_postgres
def test_telegram_bot_group_ai_settings_rejects_account_coverage_writes() -> None:
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

        with pytest.raises(ValueError, match="请到 Web 任务详情编辑"):
            apply_group_ai_settings_from_bot(
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

        task = session.get(Task, "task-ai")

    assert "account_coverage_mode" not in task.type_config
    assert "per_account_daily_max_messages" not in task.type_config


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
def test_telegram_bot_update_command_rejects_group_ai_settings_writes() -> None:
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
    assert "TG bot 仅支持查看" in result["text"]
    assert "Web 任务详情" in result["text"]
    assert "teacher_targets" not in task.type_config


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
    assert "讨论老师数：1" in result["text"]
    keyboard_text = str(result["reply_markup"]["inline_keyboard"])
    assert "查看话题摘要" in keyboard_text
    assert "设置话题方向" in keyboard_text
    assert "设置讨论老师" in keyboard_text
    assert result["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "ai_group:summary:task-ai"


@pytest.mark.no_postgres
def test_telegram_bot_summary_callback_shows_readable_topic_package() -> None:
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
                    "topic_directions": [{"title": "郑州楼凤妹子怎么样", "weight": 2}, {"title": "主任最近约新妹子了", "weight": 1}],
                    "teacher_targets": [{"name": "花花老师身材服务真好", "priority": 2}, {"name": "新人榜单妹子", "priority": 1}],
                },
            )
        )
        session.commit()

        result = handle_group_ai_bot_update(
            session,
            tenant_id=1,
            update={"callback_query": {"message": {"chat": {"id": "1001"}}, "data": "ai_group:summary:task-ai"}},
        )

    assert "话题摘要" in result["text"]
    assert "1. 郑州楼凤妹子怎么样" in result["text"]
    assert "2. 主任最近约新妹子了" in result["text"]
    assert "讨论老师摘要" in result["text"]
    assert "1. 花花老师身材服务真好" in result["text"]
    assert "设置话题方向" in str(result["reply_markup"]["inline_keyboard"])


@pytest.mark.no_postgres
def test_telegram_bot_edit_topics_callback_saves_multiline_topics() -> None:
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
            update={"callback_query": {"message": {"chat": {"id": "1001"}}, "data": "ai_group:edit_topics:task-ai"}},
        )
        assert "请发送话题方向" in result["text"]
        conversation = session.scalar(select(TelegramBotConversation).where(TelegramBotConversation.tenant_id == 1, TelegramBotConversation.chat_id == "1001"))
        assert conversation is not None
        assert conversation.step == "topics"

        saved = handle_group_ai_bot_update(
            session,
            tenant_id=1,
            update={"message": {"chat": {"id": "1001"}, "text": "郑州楼凤妹子怎么样\n主任最近约新妹子了\n精品榜的妹子真好"}},
        )
        conversation = session.scalar(select(TelegramBotConversation).where(TelegramBotConversation.tenant_id == 1, TelegramBotConversation.chat_id == "1001"))
        task = session.get(Task, "task-ai")

    assert conversation is None
    assert "已保存话题方向 3 条" in saved["text"]
    assert [item["title"] for item in task.type_config["topic_directions"]] == ["郑州楼凤妹子怎么样", "主任最近约新妹子了", "精品榜的妹子真好"]
    assert "topic_hint" not in task.type_config


@pytest.mark.no_postgres
def test_telegram_bot_draft_prompt_does_not_reenter_settings_buttons() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间", admin_chat_id="1001", telegram_bot_token_ciphertext="encrypted", ai_group_bot_enabled=True))
        session.add(Task(id="task-ai", tenant_id=1, name="AI 活群", type="group_ai_chat", status="running", type_config={"target_group_id": 7}))
        session.commit()

        result = handle_group_ai_bot_update(
            session,
            tenant_id=1,
            update={"callback_query": {"message": {"chat": {"id": "1001"}}, "data": "ai_group:edit_topics:task-ai"}},
        )

    keyboard_text = str(result["reply_markup"]["inline_keyboard"])
    assert "请发送话题方向" in result["text"]
    assert "设置话题方向" not in keyboard_text
    assert "设置讨论老师" not in keyboard_text
    assert "取消编辑" in keyboard_text


@pytest.mark.no_postgres
def test_telegram_bot_draft_validation_error_keeps_webhook_successful() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间", admin_chat_id="1001", telegram_bot_token_ciphertext="encrypted", ai_group_bot_enabled=True))
        session.add(Task(id="task-ai", tenant_id=1, name="AI 活群", type="group_ai_chat", status="running", type_config={"target_group_id": 7}))
        session.add(TelegramBotConversation(tenant_id=1, chat_id="1001", task_id="task-ai", step="topics", draft_config={}))
        session.commit()

        result = handle_group_ai_bot_update(
            session,
            tenant_id=1,
            update={"message": {"chat": {"id": "1001"}, "text": "话题" * 40}},
        )
        conversation = session.scalar(select(TelegramBotConversation).where(TelegramBotConversation.tenant_id == 1, TelegramBotConversation.chat_id == "1001"))
        task = session.get(Task, "task-ai")

    assert result["method"] == "sendMessage"
    assert "保存失败" in result["text"]
    assert "请修改后重新发送" in result["text"]
    assert conversation is not None
    assert "topic_directions" not in task.type_config


@pytest.mark.no_postgres
def test_telegram_bot_edit_teachers_callback_saves_multiline_discussion_teachers() -> None:
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
        session.add(
            TelegramBotConversation(
                tenant_id=1,
                chat_id="1001",
                task_id="task-ai",
                step="teachers",
                draft_config={},
            )
        )
        session.commit()

        result = handle_group_ai_bot_update(
            session,
            tenant_id=1,
            update={"message": {"chat": {"id": "1001"}, "text": "花花老师身材服务真好\n新人榜单妹子"}},
        )
        task = session.get(Task, "task-ai")
        conversation = session.scalar(select(TelegramBotConversation).where(TelegramBotConversation.tenant_id == 1, TelegramBotConversation.chat_id == "1001"))

    assert "已保存讨论老师 2 条" in result["text"]
    assert conversation is None
    assert [item["name"] for item in task.type_config["teacher_targets"]] == ["花花老师身材服务真好", "新人榜单妹子"]


@pytest.mark.no_postgres
def test_telegram_bot_legacy_coverage_callback_does_not_write_task() -> None:
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
            update={"callback_query": {"message": {"chat": {"id": "1001"}}, "data": "ai_group:edit_coverage:task-ai"}},
        )
        task = session.get(Task, "task-ai")

    assert "TG bot 仅支持查看" in result["text"]
    assert "Web 任务详情" in result["text"]
    assert "account_coverage_mode" not in task.type_config

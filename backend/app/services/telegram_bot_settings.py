from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Task, TelegramBotConversation, Tenant
from app.models.enums import now
from app.schemas.task_center import GroupAIChatTaskConfigUpdate
from app.admin_chats import admin_chat_is_allowed
from app.services.task_center.service import update_group_ai_chat_config

CONVERSATION_TTL = timedelta(minutes=30)


def apply_group_ai_settings_from_bot(
    session: Session,
    *,
    tenant_id: int,
    chat_id: str,
    task_id: str,
    payload: dict[str, Any],
) -> Task:
    tenant = session.get(Tenant, tenant_id)
    if not tenant or not tenant.telegram_bot_configured or not tenant.admin_chat_id:
        raise PermissionError("Telegram Bot 未配置或缺少管理员 chat id")
    if not tenant.ai_group_bot_enabled:
        raise PermissionError("AI 活群 Bot 设置未启用")
    if not admin_chat_is_allowed(tenant.admin_chat_id, chat_id):
        raise PermissionError("只有租户管理员 chat id 可以修改 AI 活群设置")
    task = session.get(Task, task_id)
    if not task or task.tenant_id != tenant_id or task.type != "group_ai_chat":
        raise ValueError("AI 活群任务不存在")
    merged = {**(task.type_config or {}), **(payload or {})}
    update_payload = GroupAIChatTaskConfigUpdate(**merged)
    return update_group_ai_chat_config(session, tenant_id, task_id, update_payload, "telegram-bot")


def handle_group_ai_bot_update(session: Session, *, tenant_id: int, update: dict[str, Any]) -> dict[str, Any]:
    if isinstance(update.get("callback_query"), dict):
        return _handle_callback_query(session, tenant_id, update["callback_query"])
    message = update.get("message") if isinstance(update.get("message"), dict) else {}
    chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
    chat_id = str(chat.get("id") or "").strip()
    text = str(message.get("text") or "").strip()
    tenant = _tenant_or_error(session, tenant_id)
    if not admin_chat_is_allowed(tenant.admin_chat_id, chat_id):
        _audit_rejected_chat(session, tenant, chat_id)
        return _reply(chat_id, "当前聊天未授权，请在 Web 系统配置中添加 Admin Chat ID。")
    if text == "/start":
        return _reply(chat_id, _start_text(tenant), _main_menu_keyboard(tenant))
    if text == "/admin":
        return _reply(chat_id, _admin_status_text(tenant), _main_menu_keyboard(tenant))
    if text == "/ai_group":
        if not tenant.ai_group_bot_enabled:
            return _reply(chat_id, "Bot 已连接，但 AI 活群 Bot 设置未启用。请到 Web 系统配置开启。")
        return _reply(chat_id, "请选择 AI 活群任务", _main_menu_keyboard(tenant))
    if not tenant.ai_group_bot_enabled:
        return _reply(chat_id, "Bot 已连接，但 AI 活群 Bot 设置未启用。请到 Web 系统配置开启后再管理任务。")
    if text == "/ai_group_tasks":
        return _reply(chat_id, _task_list_text(session, tenant_id), _task_list_keyboard(session, tenant_id))
    if text.startswith("/ai_group_settings "):
        task_id = text.split(maxsplit=1)[1].strip()
        return _reply(chat_id, _task_settings_text(session, tenant_id, task_id))
    if text.startswith("/ai_group_set "):
        task_id, raw_payload = _split_set_command(text)
        task = apply_group_ai_settings_from_bot(
            session,
            tenant_id=tenant_id,
            chat_id=chat_id,
            task_id=task_id,
            payload=_json_payload(raw_payload),
        )
        return _reply(chat_id, f"已保存 AI 活群设置：{task.name} ({task.id})")
    draft_reply = _handle_draft_message(session, tenant_id, chat_id, text)
    if draft_reply:
        return draft_reply
    return _reply(chat_id, "可用命令：/ai_group_tasks、/ai_group_settings <task_id>、/ai_group_set <task_id> <json>", _main_menu_keyboard())


def _assert_admin_chat(session: Session, tenant_id: int, chat_id: str) -> Tenant:
    tenant = _tenant_or_error(session, tenant_id)
    if not tenant.ai_group_bot_enabled:
        raise PermissionError("AI 活群 Bot 设置未启用")
    if not admin_chat_is_allowed(tenant.admin_chat_id, chat_id):
        raise PermissionError("只有租户管理员 chat id 可以修改 AI 活群设置")
    return tenant


def _tenant_or_error(session: Session, tenant_id: int) -> Tenant:
    tenant = session.get(Tenant, tenant_id)
    if not tenant or not tenant.telegram_bot_configured or not tenant.admin_chat_id:
        raise PermissionError("Telegram Bot 未配置或缺少管理员 chat id")
    return tenant



def _audit_rejected_chat(session: Session, tenant: Tenant, chat_id: str) -> None:
    from app.services._common import audit

    audit(
        session,
        tenant_id=tenant.id,
        actor="telegram-bot",
        action="TG Bot未授权聊天拒绝",
        target_type="telegram_chat",
        target_id=chat_id or "-",
        detail="chat_id 不在 admin_chat_id 列表",
    )
    session.commit()


def _start_text(tenant: Tenant) -> str:
    if tenant.ai_group_bot_enabled:
        return "Bot 已连接。可使用 /admin 查看状态，或使用 /ai_group 管理 AI 活群任务。"
    return "Bot 已连接，但 AI 活群 Bot 设置未启用。请到 Web 系统配置开启。"


def _admin_status_text(tenant: Tenant) -> str:
    ai_status = "已启用" if tenant.ai_group_bot_enabled else "AI 活群 Bot 设置未启用"
    return "\n".join(
        [
            "Bot 已连接",
            f"Webhook：{tenant.telegram_bot_webhook_status or 'not_configured'}",
            f"AI 活群：{ai_status}",
        ]
    )


def _task_list_text(session: Session, tenant_id: int) -> str:
    tasks = session.scalars(
        select(Task).where(Task.tenant_id == tenant_id, Task.type == "group_ai_chat", Task.deleted_at.is_(None)).order_by(Task.updated_at.desc()).limit(20)
    ).all()
    if not tasks:
        return "暂无 AI 活群任务"
    return "\n".join(f"{task.id} | {task.name} | {task.status}" for task in tasks)


def _task_settings_text(session: Session, tenant_id: int, task_id: str) -> str:
    task = session.get(Task, task_id)
    if not task or task.tenant_id != tenant_id or task.type != "group_ai_chat":
        raise ValueError("AI 活群任务不存在")
    config = task.type_config or {}
    return "\n".join(
        [
            f"任务：{task.name} ({task.id})",
            f"话题数：{len(config.get('topic_directions') or [])}",
            f"老师数：{len(config.get('teacher_targets') or [])}",
            f"话题：{config.get('topic_directions') or config.get('topic_hint') or '-'}",
            f"老师：{config.get('teacher_targets') or '-'}",
            f"连发：{config.get('consecutive_message_enabled', False)} {config.get('consecutive_message_min', 2)}-{config.get('consecutive_message_max', 4)}",
            f"全账号日覆盖：{config.get('account_coverage_mode', 'natural')} {config.get('per_account_daily_min_messages', 1)}-{config.get('per_account_daily_max_messages', 2)}",
        ]
    )


def _handle_callback_query(session: Session, tenant_id: int, callback_query: dict[str, Any]) -> dict[str, Any]:
    message = callback_query.get("message") if isinstance(callback_query.get("message"), dict) else {}
    chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
    chat_id = str(chat.get("id") or "").strip()
    data = str(callback_query.get("data") or "").strip()
    _assert_admin_chat(session, tenant_id, chat_id)
    if data == "ai_group:tasks":
        return _reply(chat_id, _task_list_text(session, tenant_id), _task_list_keyboard(session, tenant_id))
    if data.startswith("ai_group:task:"):
        task_id = data.rsplit(":", 1)[-1]
        return _reply(chat_id, _task_settings_text(session, tenant_id, task_id), _task_settings_keyboard(task_id))
    if data.startswith("ai_group:edit_topics:"):
        return _start_edit(session, tenant_id, chat_id, data.rsplit(":", 1)[-1], "topics")
    if data.startswith("ai_group:edit_teachers:"):
        return _start_edit(session, tenant_id, chat_id, data.rsplit(":", 1)[-1], "teachers")
    if data.startswith("ai_group:edit_burst:"):
        return _start_edit(session, tenant_id, chat_id, data.rsplit(":", 1)[-1], "burst")
    if data.startswith("ai_group:edit_coverage:"):
        return _start_edit(session, tenant_id, chat_id, data.rsplit(":", 1)[-1], "coverage")
    if data.startswith("ai_group:confirm:"):
        return _confirm_draft(session, tenant_id, chat_id, data.rsplit(":", 1)[-1])
    if data.startswith("ai_group:cancel:"):
        return _cancel_draft(session, tenant_id, chat_id)
    return _reply(chat_id, "无法识别的操作，请重新选择。", _main_menu_keyboard())


def _main_menu_keyboard(tenant: Tenant | None = None) -> dict[str, Any]:
    if tenant is not None and not tenant.ai_group_bot_enabled:
        return {"inline_keyboard": []}
    return {"inline_keyboard": [[{"text": "AI 活群任务", "callback_data": "ai_group:tasks"}]]}


def _task_list_keyboard(session: Session, tenant_id: int) -> dict[str, Any]:
    tasks = session.scalars(
        select(Task).where(Task.tenant_id == tenant_id, Task.type == "group_ai_chat", Task.deleted_at.is_(None)).order_by(Task.updated_at.desc()).limit(20)
    ).all()
    return {"inline_keyboard": [[{"text": task.name[:40], "callback_data": f"ai_group:task:{task.id}"}] for task in tasks]}


def _task_settings_keyboard(task_id: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": "设置话题方向", "callback_data": f"ai_group:edit_topics:{task_id}"}],
            [{"text": "设置聊天对象老师", "callback_data": f"ai_group:edit_teachers:{task_id}"}],
            [{"text": "设置同账号连发", "callback_data": f"ai_group:edit_burst:{task_id}"}],
            [{"text": "设置全账号日覆盖", "callback_data": f"ai_group:edit_coverage:{task_id}"}],
        ]
    }


def _start_edit(session: Session, tenant_id: int, chat_id: str, task_id: str, step: str) -> dict[str, Any]:
    task = _task_or_error(session, tenant_id, task_id)
    draft = dict(task.type_config or {})
    conversation = _conversation(session, tenant_id, chat_id) or TelegramBotConversation(tenant_id=tenant_id, chat_id=chat_id)
    conversation.task_id = task.id
    conversation.step = step
    conversation.draft_config = draft
    conversation.updated_at = now()
    session.add(conversation)
    session.commit()
    return _reply(chat_id, _edit_prompt(step, task.id), _cancel_keyboard(task.id))


def _handle_draft_message(session: Session, tenant_id: int, chat_id: str, text: str) -> dict[str, Any] | None:
    conversation = _active_conversation(session, tenant_id, chat_id)
    if not conversation:
        return None
    draft = _draft_with_step(conversation.draft_config, conversation.step, text)
    conversation.draft_config = draft
    conversation.step = "confirm"
    conversation.updated_at = now()
    session.commit()
    return _reply(chat_id, _draft_summary(conversation.task_id, draft), _confirm_keyboard(conversation.task_id))


def _confirm_draft(session: Session, tenant_id: int, chat_id: str, task_id: str) -> dict[str, Any]:
    conversation = _active_conversation(session, tenant_id, chat_id)
    if not conversation or conversation.task_id != task_id or conversation.step != "confirm":
        raise ValueError("没有可保存的草稿，请重新选择任务")
    task = apply_group_ai_settings_from_bot(session, tenant_id=tenant_id, chat_id=chat_id, task_id=task_id, payload=conversation.draft_config)
    session.delete(conversation)
    session.commit()
    return _reply(chat_id, f"已保存 AI 活群设置：{task.name} ({task.id})", _task_settings_keyboard(task.id))


def _cancel_draft(session: Session, tenant_id: int, chat_id: str) -> dict[str, Any]:
    conversation = _conversation(session, tenant_id, chat_id)
    task_id = conversation.task_id if conversation else ""
    if conversation:
        session.delete(conversation)
        session.commit()
    keyboard = _task_settings_keyboard(task_id) if task_id else _main_menu_keyboard()
    return _reply(chat_id, "已取消本次编辑。", keyboard)


def _split_set_command(text: str) -> tuple[str, str]:
    parts = text.split(maxsplit=2)
    if len(parts) != 3:
        raise ValueError("格式：/ai_group_set <task_id> <json>")
    return parts[1], parts[2]


def _json_payload(raw_payload: str) -> dict[str, Any]:
    import json

    data = json.loads(raw_payload)
    if not isinstance(data, dict):
        raise ValueError("设置内容必须是 JSON object")
    return data


def _draft_with_step(draft: dict[str, Any], step: str, text: str) -> dict[str, Any]:
    next_draft = dict(draft or {})
    if step == "topics":
        next_draft["topic_directions"] = _parse_topics(text)
        return next_draft
    if step == "teachers":
        next_draft["teacher_targets"] = _parse_teachers(text)
        return next_draft
    if step == "burst":
        next_draft.update(_parse_burst(text))
        return next_draft
    if step == "coverage":
        next_draft.update(_parse_coverage(text))
        return next_draft
    raise ValueError("草稿步骤无效，请重新选择任务")


def _parse_topics(text: str) -> list[dict[str, Any]]:
    topics = []
    for line in _non_empty_lines(text):
        parts = [part.strip() for part in line.split("|")]
        if len(parts) == 2:
            title, weight = parts
            topics.append({"title": title, "weight": float(weight)})
        elif len(parts) == 3:
            title, description, weight = parts
            topics.append({"title": title, "description": description, "weight": float(weight)})
        else:
            raise ValueError("话题格式：标题|权重 或 标题|描述|权重")
    return topics


def _parse_teachers(text: str) -> list[dict[str, Any]]:
    teachers = []
    for line in _non_empty_lines(text):
        parts = [part.strip() for part in line.split("|")]
        if len(parts) == 2:
            name, priority = parts
            teachers.append({"name": name, "priority": int(priority)})
        elif len(parts) == 3:
            name, description, priority = parts
            teachers.append({"name": name, "description": description, "priority": int(priority)})
        else:
            raise ValueError("老师格式：姓名|优先级 或 姓名|描述|优先级")
    return teachers


def _parse_burst(text: str) -> dict[str, Any]:
    parts = text.split()
    enabled = parts[0].lower() in {"on", "true", "1", "开", "开启"}
    if not enabled:
        return {"consecutive_message_enabled": False}
    if len(parts) != 4:
        raise ValueError("连发格式：开启 2 4 0.3，或 关闭")
    return {
        "consecutive_message_enabled": True,
        "consecutive_message_min": int(parts[1]),
        "consecutive_message_max": int(parts[2]),
        "consecutive_message_probability": float(parts[3]),
    }


def _parse_coverage(text: str) -> dict[str, Any]:
    parts = text.split()
    enabled = parts[0].lower() in {"on", "true", "1", "开", "开启"}
    if not enabled:
        return {"account_coverage_mode": "natural", "coverage_window_hours": 24}
    if len(parts) != 3:
        raise ValueError("全账号日覆盖格式：开启 1 2，或 关闭")
    return {
        "account_coverage_mode": "all_accounts_daily",
        "per_account_daily_min_messages": int(parts[1]),
        "per_account_daily_max_messages": int(parts[2]),
        "coverage_window_hours": 24,
    }


def _non_empty_lines(text: str) -> list[str]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        raise ValueError("输入不能为空")
    return lines


def _active_conversation(session: Session, tenant_id: int, chat_id: str) -> TelegramBotConversation | None:
    conversation = _conversation(session, tenant_id, chat_id)
    if not conversation:
        return None
    if now() - conversation.updated_at > CONVERSATION_TTL:
        session.delete(conversation)
        session.commit()
        raise ValueError("草稿已超时，请重新选择任务")
    return conversation


def _conversation(session: Session, tenant_id: int, chat_id: str) -> TelegramBotConversation | None:
    return session.scalar(select(TelegramBotConversation).where(TelegramBotConversation.tenant_id == tenant_id, TelegramBotConversation.chat_id == chat_id))


def _task_or_error(session: Session, tenant_id: int, task_id: str) -> Task:
    task = session.get(Task, task_id)
    if not task or task.tenant_id != tenant_id or task.type != "group_ai_chat":
        raise ValueError("AI 活群任务不存在")
    return task


def _edit_prompt(step: str, task_id: str) -> str:
    prompts = {
        "topics": f"请输入任务 {task_id} 的话题方向，每行一个：标题|描述|权重。描述可省略为：标题|权重。",
        "teachers": f"请输入任务 {task_id} 的聊天对象老师，每行一个：姓名|描述|优先级。描述可省略为：姓名|优先级。",
        "burst": f"请输入任务 {task_id} 的同账号连发：开启 2 4 0.3，或输入 关闭。",
        "coverage": f"请输入任务 {task_id} 的全账号日覆盖：开启 1 2，或输入 关闭。",
    }
    return prompts[step]


def _draft_summary(task_id: str, draft: dict[str, Any]) -> str:
    return "\n".join(
        [
            f"待保存任务：{task_id}",
            f"话题数：{len(draft.get('topic_directions') or [])}",
            f"老师数：{len(draft.get('teacher_targets') or [])}",
            f"连发：{draft.get('consecutive_message_enabled', False)} {draft.get('consecutive_message_min', 2)}-{draft.get('consecutive_message_max', 4)}",
            f"全账号日覆盖：{draft.get('account_coverage_mode', 'natural')} {draft.get('per_account_daily_min_messages', 1)}-{draft.get('per_account_daily_max_messages', 2)}",
            "确认后写入任务配置。",
        ]
    )


def _confirm_keyboard(task_id: str) -> dict[str, Any]:
    return {"inline_keyboard": [[{"text": "确认保存", "callback_data": f"ai_group:confirm:{task_id}"}], [{"text": "取消", "callback_data": f"ai_group:cancel:{task_id}"}]]}


def _cancel_keyboard(task_id: str) -> dict[str, Any]:
    return {"inline_keyboard": [[{"text": "取消", "callback_data": f"ai_group:cancel:{task_id}"}]]}


def _reply(chat_id: str, text: str, reply_markup: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = {"method": "sendMessage", "chat_id": chat_id, "text": text[:3500], "disable_web_page_preview": True}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return payload

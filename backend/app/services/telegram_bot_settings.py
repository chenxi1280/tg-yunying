from __future__ import annotations

from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Task, TelegramBotConversation, Tenant
from app.admin_chats import admin_chat_is_allowed
from app.config import get_settings
from app.schemas.task_center import GroupAIChatConfig

CONFIG_WRITE_UNSUPPORTED_MESSAGE = "TG bot 仅支持查看摘要，以及配置话题方向和讨论老师；其它完整配置请到 Web 任务详情编辑。"
BOT_WRITABLE_FIELDS = frozenset({"topic_directions", "teacher_targets"})
SUMMARY_LIMIT = 8


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
    unsupported = sorted(set(payload) - BOT_WRITABLE_FIELDS)
    if unsupported:
        raise ValueError(CONFIG_WRITE_UNSUPPORTED_MESSAGE)
    next_config = {**(task.type_config or {}), **payload}
    next_config.pop("topic_hint", None)
    normalized = GroupAIChatConfig(**next_config).model_dump(mode="json", exclude_none=True)
    task.type_config = normalized
    session.commit()
    session.refresh(task)
    return task


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
        return _reply(chat_id, _task_settings_text(session, tenant_id, task_id), _task_settings_keyboard(task_id))
    if text == "/cancel":
        return _cancel_draft(session, tenant_id, chat_id)
    if text.startswith("/ai_group_set "):
        return _reply(chat_id, CONFIG_WRITE_UNSUPPORTED_MESSAGE, _main_menu_keyboard(tenant))
    draft_reply = _handle_draft_message(session, tenant_id, chat_id, text)
    if draft_reply:
        return draft_reply
    return _reply(chat_id, "可用命令：/ai_group_tasks、/ai_group_settings <task_id>。完整配置请到 Web 任务详情编辑。", _main_menu_keyboard())


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
    topics = _topic_titles(config)
    targets = _target_names(config)
    return "\n".join(
        [
            f"任务：{task.name} ({task.id})",
            f"话题数：{len(topics)}",
            f"讨论老师数：{len(targets)}",
            f"话题摘要：{_compact_list(topics) or '-'}",
            f"讨论老师摘要：{_compact_list(targets) or '-'}",
            f"连发：{config.get('consecutive_message_enabled', False)} {config.get('consecutive_message_min', 2)}-{config.get('consecutive_message_max', 4)}",
            f"全账号日覆盖：{config.get('account_coverage_mode', 'natural')} {config.get('per_account_daily_min_messages', 1)}-{config.get('per_account_daily_max_messages', 2)}",
            "Bot 可设置：话题方向、讨论老师；其它配置请到 Web 任务详情。",
        ]
    )


def _topic_summary_text(session: Session, tenant_id: int, task_id: str) -> str:
    task = _task_or_error(session, tenant_id, task_id)
    config = task.type_config or {}
    topics = _numbered_lines(_topic_titles(config))
    targets = _numbered_lines(_target_names(config))
    return "\n".join(
        [
            f"任务：{task.name} ({task.id})",
            "话题摘要",
            topics or "-",
            "讨论老师摘要",
            targets or "-",
            "可在 Bot 中继续设置话题方向和讨论老师。",
        ]
    )


def _topic_titles(config: dict[str, Any]) -> list[str]:
    topics = config.get("topic_directions") or []
    titles = [str(item.get("title") or "").strip() for item in topics if isinstance(item, dict)]
    return [title for title in titles if title]


def _target_names(config: dict[str, Any]) -> list[str]:
    targets = config.get("teacher_targets") or []
    names = [str(item.get("name") or "").strip() for item in targets if isinstance(item, dict)]
    return [name for name in names if name]


def _compact_list(items: list[str]) -> str:
    visible = items[:3]
    suffix = f" 等 {len(items)} 条" if len(items) > 3 else ""
    return "、".join(visible) + suffix


def _numbered_lines(items: list[str]) -> str:
    return "\n".join(f"{index}. {item}" for index, item in enumerate(items[:SUMMARY_LIMIT], start=1))


def _web_edit_text() -> str:
    url = _web_task_center_url()
    if url:
        return f"请打开 Web 任务中心编辑该任务：{url}"
    return "请到 Web 任务中心的任务详情里编辑该任务。"


def _web_task_center_url() -> str:
    base_url = get_settings().public_app_base_url
    return f"{base_url}/task-center" if base_url else ""


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
    if data.startswith("ai_group:summary:"):
        task_id = data.rsplit(":", 1)[-1]
        return _reply(chat_id, _topic_summary_text(session, tenant_id, task_id), _task_settings_keyboard(task_id))
    if data.startswith("ai_group:web_edit:"):
        task_id = data.rsplit(":", 1)[-1]
        _task_or_error(session, tenant_id, task_id)
        return _reply(chat_id, _web_edit_text(), _task_settings_keyboard(task_id))
    if data.startswith("ai_group:edit_topics:"):
        task_id = data.rsplit(":", 1)[-1]
        return _start_draft(session, tenant_id, chat_id, task_id, "topics")
    if data.startswith("ai_group:edit_teachers:"):
        task_id = data.rsplit(":", 1)[-1]
        return _start_draft(session, tenant_id, chat_id, task_id, "teachers")
    if data.startswith("ai_group:edit_burst:"):
        return _reply(chat_id, CONFIG_WRITE_UNSUPPORTED_MESSAGE, _task_settings_keyboard(data.rsplit(":", 1)[-1]))
    if data.startswith("ai_group:edit_coverage:"):
        return _reply(chat_id, CONFIG_WRITE_UNSUPPORTED_MESSAGE, _task_settings_keyboard(data.rsplit(":", 1)[-1]))
    if data.startswith("ai_group:confirm:"):
        return _reply(chat_id, CONFIG_WRITE_UNSUPPORTED_MESSAGE, _task_settings_keyboard(data.rsplit(":", 1)[-1]))
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
    keyboard = [[{"text": "查看话题摘要", "callback_data": f"ai_group:summary:{task_id}"}]]
    keyboard.append([{"text": "设置话题方向", "callback_data": f"ai_group:edit_topics:{task_id}"}])
    keyboard.append([{"text": "设置讨论老师", "callback_data": f"ai_group:edit_teachers:{task_id}"}])
    edit_url = _web_task_center_url()
    if edit_url:
        keyboard.append([{"text": "打开 Web 编辑", "url": edit_url}])
    else:
        keyboard.append([{"text": "打开 Web 编辑", "callback_data": f"ai_group:web_edit:{task_id}"}])
    keyboard.append([{"text": "返回任务列表", "callback_data": "ai_group:tasks"}])
    return {"inline_keyboard": keyboard}


def _handle_draft_message(session: Session, tenant_id: int, chat_id: str, text: str) -> dict[str, Any] | None:
    conversation = _conversation(session, tenant_id, chat_id)
    if not conversation:
        return None
    task_id = conversation.task_id
    if not text.strip():
        return _reply(chat_id, "内容不能为空，请按每行一个继续发送。", _draft_keyboard(task_id))
    if conversation.step == "topics":
        task = _save_draft_settings(session, tenant_id=tenant_id, chat_id=chat_id, task_id=task_id, payload={"topic_directions": text})
        if isinstance(task, str):
            return _reply(chat_id, task, _draft_keyboard(task_id))
        count = len(_topic_titles(task.type_config or {}))
        session.delete(conversation)
        session.commit()
        return _reply(chat_id, f"已保存话题方向 {count} 条。", _task_settings_keyboard(task_id))
    if conversation.step == "teachers":
        task = _save_draft_settings(session, tenant_id=tenant_id, chat_id=chat_id, task_id=task_id, payload={"teacher_targets": text})
        if isinstance(task, str):
            return _reply(chat_id, task, _draft_keyboard(task_id))
        count = len(_target_names(task.type_config or {}))
        session.delete(conversation)
        session.commit()
        return _reply(chat_id, f"已保存讨论老师 {count} 条。", _task_settings_keyboard(task_id))
    session.delete(conversation)
    session.commit()
    return _reply(chat_id, f"旧草稿已取消。{CONFIG_WRITE_UNSUPPORTED_MESSAGE}", _task_settings_keyboard(task_id))


def _start_draft(session: Session, tenant_id: int, chat_id: str, task_id: str, step: str) -> dict[str, Any]:
    _task_or_error(session, tenant_id, task_id)
    existing = _conversation(session, tenant_id, chat_id)
    if existing:
        session.delete(existing)
        session.flush()
    session.add(TelegramBotConversation(tenant_id=tenant_id, chat_id=chat_id, task_id=task_id, step=step, draft_config={}))
    session.commit()
    if step == "topics":
        text = "请发送话题方向，每行一个，越靠前权重越高。"
    else:
        text = "请发送讨论老师，每行一个，越靠前优先级越高。"
    return _reply(chat_id, f"{text}\n发送 /cancel 可取消。", _draft_keyboard(task_id))


def _cancel_draft(session: Session, tenant_id: int, chat_id: str) -> dict[str, Any]:
    conversation = _conversation(session, tenant_id, chat_id)
    task_id = conversation.task_id if conversation else ""
    if conversation:
        session.delete(conversation)
        session.commit()
    keyboard = _task_settings_keyboard(task_id) if task_id else _main_menu_keyboard()
    return _reply(chat_id, "已取消本次编辑。", keyboard)


def _conversation(session: Session, tenant_id: int, chat_id: str) -> TelegramBotConversation | None:
    return session.scalar(select(TelegramBotConversation).where(TelegramBotConversation.tenant_id == tenant_id, TelegramBotConversation.chat_id == chat_id))


def _task_or_error(session: Session, tenant_id: int, task_id: str) -> Task:
    task = session.get(Task, task_id)
    if not task or task.tenant_id != tenant_id or task.type != "group_ai_chat":
        raise ValueError("AI 活群任务不存在")
    return task


def _save_draft_settings(
    session: Session,
    *,
    tenant_id: int,
    chat_id: str,
    task_id: str,
    payload: dict[str, Any],
) -> Task | str:
    try:
        return apply_group_ai_settings_from_bot(session, tenant_id=tenant_id, chat_id=chat_id, task_id=task_id, payload=payload)
    except (ValueError, ValidationError) as exc:
        session.rollback()
        return f"保存失败：{_validation_message(exc)}。请修改后重新发送，或发送 /cancel 取消。"


def _validation_message(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        errors = exc.errors()
        if errors:
            return str(errors[0].get("msg") or "配置格式不正确")
    return str(exc)


def _draft_keyboard(task_id: str) -> dict[str, Any]:
    return {"inline_keyboard": [[{"text": "取消编辑", "callback_data": f"ai_group:cancel:{task_id}"}]]}


def _reply(chat_id: str, text: str, reply_markup: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = {"method": "sendMessage", "chat_id": chat_id, "text": text[:3500], "disable_web_page_preview": True}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return payload

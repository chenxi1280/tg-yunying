from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Task, Tenant
from app.schemas.task_center import GroupAIChatTaskConfigUpdate
from app.services.task_center.service import update_group_ai_chat_config


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
    if str(chat_id).strip() != str(tenant.admin_chat_id).strip():
        raise PermissionError("只有租户管理员 chat id 可以修改 AI 活群设置")
    task = session.get(Task, task_id)
    if not task or task.tenant_id != tenant_id or task.type != "group_ai_chat":
        raise ValueError("AI 活群任务不存在")
    merged = {**(task.type_config or {}), **(payload or {})}
    update_payload = GroupAIChatTaskConfigUpdate(**merged)
    return update_group_ai_chat_config(session, tenant_id, task_id, update_payload, "telegram-bot")


def handle_group_ai_bot_update(session: Session, *, tenant_id: int, update: dict[str, Any]) -> dict[str, Any]:
    message = update.get("message") if isinstance(update.get("message"), dict) else {}
    chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
    chat_id = str(chat.get("id") or "").strip()
    text = str(message.get("text") or "").strip()
    _assert_admin_chat(session, tenant_id, chat_id)
    if text == "/ai_group_tasks":
        return _reply(chat_id, _task_list_text(session, tenant_id))
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
    return _reply(chat_id, "可用命令：/ai_group_tasks、/ai_group_settings <task_id>、/ai_group_set <task_id> <json>")


def _assert_admin_chat(session: Session, tenant_id: int, chat_id: str) -> Tenant:
    tenant = session.get(Tenant, tenant_id)
    if not tenant or not tenant.telegram_bot_configured or not tenant.admin_chat_id:
        raise PermissionError("Telegram Bot 未配置或缺少管理员 chat id")
    if str(chat_id).strip() != str(tenant.admin_chat_id).strip():
        raise PermissionError("只有租户管理员 chat id 可以修改 AI 活群设置")
    return tenant


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
            f"话题：{config.get('topic_directions') or config.get('topic_hint') or '-'}",
            f"老师：{config.get('teacher_targets') or '-'}",
            f"连发：{config.get('consecutive_message_enabled', False)} {config.get('consecutive_message_min', 2)}-{config.get('consecutive_message_max', 4)}",
        ]
    )


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


def _reply(chat_id: str, text: str) -> dict[str, Any]:
    return {"method": "sendMessage", "chat_id": chat_id, "text": text[:3500], "disable_web_page_preview": True}

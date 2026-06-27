from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.database import get_session
from app.schemas import TaskOut
from app.services.telegram_bot_settings import apply_group_ai_settings_from_bot, handle_group_ai_bot_update


class GroupAISettingsBotRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: int = Field(default=1, ge=1)
    chat_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)


class TelegramBotUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    tenant_id: int = Field(default=1, ge=1)
    update: dict[str, Any] = Field(default_factory=dict)


router = APIRouter()


@router.post("/api/telegram-bot/tasks/group-ai-chat/settings", response_model=TaskOut)
def post_group_ai_settings_from_bot(payload: GroupAISettingsBotRequest, session: Session = Depends(get_session)):
    try:
        return apply_group_ai_settings_from_bot(
            session,
            tenant_id=payload.tenant_id,
            chat_id=payload.chat_id,
            task_id=payload.task_id,
            payload=payload.payload,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/telegram-bot/update")
def post_telegram_bot_update(payload: TelegramBotUpdateRequest, session: Session = Depends(get_session)):
    try:
        return handle_group_ai_bot_update(session, tenant_id=payload.tenant_id, update=payload.update)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    Action,
    ContentMixCycleSlot,
    ContentMixObligation,
    Task,
    TaskDayLedger,
    TaskGroupDailyTarget,
    TaskGroupDailyMessageSlot,
    TenantAiSetting,
)
from app.services._common import _now
from app.timezone import BEIJING_TZ

from .ai_generator import AI_CONTENT_REQUEST_TIMEOUT_SECONDS
from .payloads import SendMessagePayload


GenerationSlotBuilder = Callable[..., dict]


def build_runtime_config(
    session: Session,
    task: Task,
    batch: list[tuple[Action, SendMessagePayload]],
    *,
    generation_slot_builder: GenerationSlotBuilder,
) -> dict:
    config = dict(task.type_config or {})
    _bind_fact_first_provider(session, task, config)
    config["account_personas"] = payload_map(batch, "account_role")
    config["account_memories"] = payload_map(batch, "account_memory")
    config["account_profiles"] = payload_map(batch, "account_profile")
    config["generation_slots"] = [
        generation_slot_builder(
            action,
            payload,
            index,
            content_obligation_fallback_ready=_content_obligation_fallback_ready(session, action),
        )
        for index, (action, payload) in enumerate(batch, 1)
    ]
    deadline = _latest_safe_send_at(session, batch[0][0])
    if deadline:
        config["_ai_generation_latest_safe_send_at"] = deadline.isoformat()
    if _due_catch_up_required(session, batch[0][0], batch[0][1]):
        config["_ai_group_due_catch_up_required"] = True
    first = batch[0][1]
    if first.topic_thread:
        config["topic_thread"] = first.topic_thread
    if first.topic_plan:
        config["topic_plan"] = first.topic_plan
    return config


def _bind_fact_first_provider(
    session: Session,
    task: Task,
    config: dict,
) -> None:
    if getattr(task, "fulfillment_contract_version", "legacy_v1") != "fact_first_v3":
        return
    setting = session.scalar(
        select(TenantAiSetting).where(TenantAiSetting.tenant_id == task.tenant_id)
    )
    provider_id = int(setting.default_provider_id or 0) if setting else 0
    if not provider_id:
        raise ValueError("fact_first_ai_provider_required")
    config["ai_provider_id"] = provider_id
    config["provider_binding_policy"] = "single_provider_key"
    if int(dict(task.type_config or {}).get("ai_provider_id") or 0) != provider_id:
        task.type_config = {**dict(task.type_config or {}), "ai_provider_id": provider_id}


def tenant_fallback_flags(task: Task) -> dict:
    session = task._sa_instance_state.session
    setting = session.scalar(
        select(TenantAiSetting).where(TenantAiSetting.tenant_id == task.tenant_id)
    ) if session is not None else None
    return {
        "_ai_group_model_fallback_enabled": bool(
            setting.ai_group_model_fallback_enabled if setting else True
        ),
        "_ai_group_grok_fallback_enabled": bool(
            setting.ai_group_grok_fallback_enabled if setting else False
        ),
        "_ai_group_static_fallback_enabled": bool(
            setting.ai_group_static_fallback_enabled if setting else True
        ),
    }


def payload_map(batch: list[tuple[Action, SendMessagePayload]], attr: str) -> dict[str, str]:
    return {
        str(action.account_id): value
        for action, payload in batch
        if action.account_id and (value := str(getattr(payload, attr) or "").strip())
    }


def quality_snapshot(payload: SendMessagePayload) -> dict:
    return {"account_profile": payload.account_profile, "stance_summary": payload.stance_summary}


def _content_obligation_fallback_ready(session: Session, action: Action) -> bool:
    cycle_slot_id = str(action.content_mix_cycle_slot_id or "")
    if not action.primary_quantity_slot_id or not cycle_slot_id:
        return False
    cycle_slot = session.get(ContentMixCycleSlot, cycle_slot_id)
    if cycle_slot is None or cycle_slot.current_action_id != action.id:
        return False
    pending_count = session.scalar(select(func.count(ContentMixObligation.id)).where(
        ContentMixObligation.assigned_cycle_slot_id == cycle_slot_id,
        ContentMixObligation.status == "pending",
    ))
    return int(pending_count or 0) == 0


def _latest_safe_send_at(session: Session, action: Action) -> datetime | None:
    quantity_slot_id = str(action.primary_quantity_slot_id or "")
    if not quantity_slot_id:
        return None
    deadline = session.scalar(select(TaskDayLedger.deadline_at).join(
        TaskGroupDailyMessageSlot,
        TaskGroupDailyMessageSlot.task_day_ledger_id == TaskDayLedger.id,
    ).where(TaskGroupDailyMessageSlot.id == quantity_slot_id))
    if deadline is None:
        return None
    aware = deadline.replace(tzinfo=timezone.utc) if deadline.tzinfo is None else deadline
    return aware.astimezone(BEIJING_TZ).replace(tzinfo=None)


def _due_catch_up_required(
    session: Session,
    action: Action,
    payload: SendMessagePayload,
) -> bool:
    target_id = str(payload.daily_group_target_id or "")
    target = session.get(TaskGroupDailyTarget, target_id) if target_id else None
    if target is None or target.due_message_count <= target.confirmed_message_count:
        return False
    overdue_seconds = (
        _naive(_now()) - _naive(action.scheduled_at)
    ).total_seconds()
    return overdue_seconds >= AI_CONTENT_REQUEST_TIMEOUT_SECONDS


def _naive(value: datetime) -> datetime:
    return value.replace(tzinfo=None) if value.tzinfo else value


__all__ = ["build_runtime_config", "payload_map", "quality_snapshot", "tenant_fallback_flags"]

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    Action,
    ContentMixCycleSlot,
    ContentMixObligation,
    Task,
    TaskGroupDailyTarget,
    TenantAiSetting,
)
from app.services._common import _now

from .ai_generator import AI_CONTENT_REQUEST_TIMEOUT_SECONDS, AiGenerationUnavailable
from .ai_content_job_binding import (
    bind_group_generation_contracts,
    enrich_group_generation_slots,
    generation_jobs_for_batch,
)
from .ai_provider_routes import (
    ProviderRouteUnavailable,
    active_route_snapshot,
    bind_generation_job_routes,
    route_config,
)
from .payloads import SendMessagePayload
from .generation_deadlines import batch_latest_safe_send_at, latest_safe_send_at as _latest_safe_send_at
from .generation_timing_binding import bind_generation_timing_config
from .ai_generation_state import cached_generation_result


GenerationSlotBuilder = Callable[..., dict]


def build_runtime_config(
    session: Session,
    task: Task,
    batch: list[tuple[Action, SendMessagePayload]],
    *,
    generation_slot_builder: GenerationSlotBuilder,
) -> dict:
    from .ai_group_content_allocation import validate_content_intent_for_gateway

    for action, payload in batch:
        validate_content_intent_for_gateway(session, payload, action=action)
    config = {**dict(task.type_config or {}), **tenant_fallback_flags(session, task)}
    _bind_fact_first_provider(session, task, config)
    config = _bind_legacy_provider_failover(session, task, config)
    _bind_legacy_attempt_job(config, batch)
    jobs = generation_jobs_for_batch(session, batch) if config.get("ai_content_route_v2_enabled") else ()
    config = bind_group_generation_contracts(session, task, batch, config=config, jobs=jobs)
    config = bind_generation_job_routes(
        session,
        jobs,
        config,
        scope_type="group",
    )
    config["account_personas"] = payload_map(batch, "account_role")
    config["account_memories"] = payload_map(batch, "account_memory")
    config["account_profiles"] = payload_map(batch, "account_profile")
    slots = [
        generation_slot_builder(
            action,
            payload,
            index,
            content_obligation_fallback_ready=_content_obligation_fallback_ready(session, action),
        )
        for index, (action, payload) in enumerate(batch, 1)
    ]
    config["generation_slots"] = enrich_group_generation_slots(config, batch, slots)
    deadline = batch_latest_safe_send_at(session, (action for action, _payload in batch))
    if deadline:
        config["_ai_generation_latest_safe_send_at"] = deadline.isoformat()
    if _due_catch_up_required(session, batch[0][0], batch[0][1]):
        config["_ai_group_due_catch_up_required"] = True
    first = batch[0][1]
    if first.topic_thread:
        config["topic_thread"] = first.topic_thread
    if first.topic_plan:
        config["topic_plan"] = first.topic_plan
    return _bind_batch_timing(session, task, batch=batch, jobs=jobs, config=config, deadline=deadline)


def _bind_batch_timing(session, task, *, batch, jobs, config, deadline):
    work = tuple((job, "response" if payload.reply_to_message_id else "proactive")
                 for job, (_action, payload) in zip(jobs, batch))
    return bind_generation_timing_config(
        session, task, work=work, config=config, deadline_at=deadline,
        requires_provider=not all(cached_generation_result(payload) for _action, payload in batch),
    )


def _bind_legacy_attempt_job(config: dict, batch: list[tuple[Action, SendMessagePayload]]) -> None:
    if config.get("ai_content_route_v2_enabled") or not batch:
        return
    job_id = str(batch[0][1].generation_job_id or "")
    if job_id:
        config["_generation_job_id"] = job_id


def _bind_legacy_provider_failover(
    session: Session,
    task: Task,
    config: dict,
) -> dict:
    if config.get("ai_content_route_v2_enabled"):
        return config
    setting = session.scalar(select(TenantAiSetting).where(
        TenantAiSetting.tenant_id == task.tenant_id,
    ))
    if not setting or not setting.ai_provider_route_fallback_enabled:
        return config
    try:
        snapshot = active_route_snapshot(session, task.tenant_id, "group_realize_general")
        inactive_ids = [item.provider.id for item in snapshot.candidates if not item.provider.is_active]
        if inactive_ids:
            ids = ",".join(str(provider_id) for provider_id in inactive_ids)
            raise ProviderRouteUnavailable(f"legacy_provider_route_inactive:{ids}")
    except ProviderRouteUnavailable as exc:
        raise AiGenerationUnavailable(f"AI 供应商优先级降级不可用：{exc}") from exc
    bound = route_config(config, snapshot)
    bound["provider_binding_policy"] = "explicit_provider_route"
    return bound


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


def tenant_fallback_flags(session: Session, task: Task) -> dict:
    config = dict(task.type_config or {})
    v2_quality = bool(
        config.get("ai_two_stage_enabled")
        or config.get("ai_content_route_v2_enabled")
    )
    setting = session.scalar(
        select(TenantAiSetting).where(TenantAiSetting.tenant_id == task.tenant_id)
    )
    return {
        "_ai_group_model_fallback_enabled": bool(
            setting.ai_group_model_fallback_enabled if setting else True
        ),
        "_ai_group_grok_fallback_enabled": bool(
            setting.ai_group_grok_fallback_enabled if setting else False
        ),
        "_ai_group_static_fallback_enabled": bool(
            setting.ai_group_static_fallback_enabled if setting else True
        ) and not v2_quality,
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

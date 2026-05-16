from __future__ import annotations

from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import GroupAuthStatus, OperationTarget, PromptTemplate, RuleSet, RuleSetVersion, TgGroup

from .config_fields import (
    CHANNEL_JITTER_FIELDS,
    GROUP_AI_LEGACY_RUNTIME_FIELDS,
    LEGACY_PACING_FIELDS,
    TYPE_CONFIG_MODELS,
)
from .utils import as_int as _as_int, as_int_list as _as_int_list


def normalize_operation_target_references(session: Session, tenant_id: int, task_type: str, config: dict[str, Any]) -> dict[str, Any]:
    next_config = dict(config)
    if task_type == "group_ai_chat":
        target_id = _as_int(next_config.get("target_operation_target_id"))
        if target_id:
            target, group = _group_for_operation_target(session, tenant_id, target_id, require_can_send=True)
            next_config["target_operation_target_id"] = target.id
            next_config["target_group_id"] = group.id
            next_config["target_group_name"] = next_config.get("target_group_name") or target.title or group.title
    elif task_type == "group_relay":
        normalized_sources: list[dict[str, Any]] = []
        for item in next_config.get("source_groups") or []:
            source = dict(item)
            target_id = _as_int(source.get("operation_target_id"))
            if target_id:
                target, group = _group_for_operation_target(session, tenant_id, target_id, require_can_send=False)
                source["operation_target_id"] = target.id
                source["group_id"] = group.id
                source["group_name"] = source.get("group_name") or target.title or group.title
            normalized_sources.append(source)
        next_config["source_groups"] = normalized_sources

        target_id = _as_int(next_config.get("target_operation_target_id"))
        target_group_ids = _as_int_list(next_config.get("target_group_ids"))
        target_operation_target_ids = _as_int_list(next_config.get("target_operation_target_ids"))
        if target_id and target_id not in target_operation_target_ids:
            target_operation_target_ids.insert(0, target_id)
        resolved_target_group_ids: list[int] = []
        for operation_target_id in target_operation_target_ids:
            target, group = _group_for_operation_target(session, tenant_id, operation_target_id, require_can_send=True)
            resolved_target_group_ids.append(group.id)
        if resolved_target_group_ids:
            next_config["target_operation_target_ids"] = target_operation_target_ids
            next_config["target_operation_target_id"] = target_operation_target_ids[0]
            next_config["target_group_id"] = resolved_target_group_ids[0]
            target_group_ids = [*resolved_target_group_ids, *target_group_ids]
        if target_group_ids:
            next_config["target_group_ids"] = list(dict.fromkeys(target_group_ids))
    return next_config


def apply_default_slang_config(session: Session, tenant_id: int, task_type: str, config: dict[str, Any]) -> dict[str, Any]:
    if task_type != "group_ai_chat" or config.get("slang_prompt_template_id") or config.get("slang_terms"):
        return config
    template_id = session.scalar(
        select(PromptTemplate.id)
        .where(
            PromptTemplate.template_type == "AI黑话词表",
            PromptTemplate.is_active.is_(True),
            or_(PromptTemplate.tenant_id == tenant_id, PromptTemplate.tenant_id.is_(None)),
        )
        .order_by(PromptTemplate.tenant_id.is_(None).asc(), PromptTemplate.id.asc())
        .limit(1)
    )
    if not template_id:
        return config
    return {**config, "slang_prompt_template_id": int(template_id)}


def validated_type_config(task_type: str, data: dict[str, Any]) -> dict[str, Any]:
    model = TYPE_CONFIG_MODELS.get(task_type)
    if not model:
        raise ValueError(f"unknown task type: {task_type}")
    normalized = model(**(data or {})).model_dump(mode="json")
    if task_type == "group_ai_chat":
        for field in GROUP_AI_LEGACY_RUNTIME_FIELDS:
            normalized.pop(field, None)
    for field in CHANNEL_JITTER_FIELDS.get(task_type, set()):
        normalized.pop(field, None)
    if task_type in {"group_relay", "channel_comment"}:
        normalized["require_review"] = False
    return normalized


def pacing_config_payload(pacing_config) -> dict[str, Any]:
    if hasattr(pacing_config, "model_dump"):
        data = pacing_config.model_dump(mode="json")
    else:
        data = dict(pacing_config or {})
    mode = data.get("mode") or "template"
    keep_legacy_fields = set()
    if mode == "fixed":
        keep_legacy_fields.update({"interval_seconds_min", "interval_seconds_max", "jitter_percent", "quiet_hours"})
    elif mode == "curve":
        keep_legacy_fields.update({"curve_type", "curve_duration_hours", "jitter_percent", "quiet_hours"})
    elif mode == "template":
        keep_legacy_fields.update({"template", "quiet_hours"})
    for field in LEGACY_PACING_FIELDS - keep_legacy_fields:
        data.pop(field, None)
    for field in list(keep_legacy_fields):
        if data.get(field) is None:
            data.pop(field, None)
    return data


def validate_rule_binding(session: Session, tenant_id: int, config: dict[str, Any]) -> None:
    rule_set_id = _as_int(config.get("rule_set_id"))
    version_id = _as_int(config.get("rule_set_version_id"))
    if version_id:
        version = session.get(RuleSetVersion, version_id)
        if not version or version.tenant_id != tenant_id:
            raise ValueError("规则版本不存在")
        if version.status != "published":
            raise ValueError("只能绑定已发布规则版本")
        if rule_set_id and version.rule_set_id != rule_set_id:
            raise ValueError("规则版本不属于所选规则集")
        return
    if rule_set_id:
        rule_set = session.get(RuleSet, rule_set_id)
        if not rule_set or rule_set.tenant_id != tenant_id:
            raise ValueError("规则集不存在")
        if not rule_set.active_version_id:
            raise ValueError("规则集没有已发布版本")
        active = session.get(RuleSetVersion, rule_set.active_version_id)
        if not active or active.tenant_id != tenant_id or active.rule_set_id != rule_set.id or active.status != "published":
            raise ValueError("规则集当前发布版本不可用")


def _group_for_operation_target(session: Session, tenant_id: int, target_id: int, *, require_can_send: bool) -> tuple[OperationTarget, TgGroup]:
    target = session.get(OperationTarget, target_id)
    if not target or target.tenant_id != tenant_id or target.target_type != "group":
        raise ValueError("运营目标不存在")
    if target.auth_status != GroupAuthStatus.AUTHORIZED.value:
        raise ValueError("运营目标未授权")
    if require_can_send and not target.can_send:
        raise ValueError("运营目标不可发送")
    group = session.scalar(
        select(TgGroup).where(
            TgGroup.tenant_id == tenant_id,
            TgGroup.tg_peer_id == target.tg_peer_id,
        )
    )
    if not group:
        raise ValueError("运营目标未关联群资产")
    return target, group


__all__ = [
    "apply_default_slang_config",
    "normalize_operation_target_references",
    "pacing_config_payload",
    "validate_rule_binding",
    "validated_type_config",
]

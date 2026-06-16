from __future__ import annotations

from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import GroupAuthStatus, OperationTarget, PromptTemplate, RuleSet, RuleSetVersion, TgGroup
from app.services._common import _now

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
        _normalize_inline_target_input(session, tenant_id, next_config, target_type="group")
        target_id = _as_int(next_config.get("target_operation_target_id"))
        if target_id:
            target, group = _group_for_operation_target(session, tenant_id, target_id, require_can_send=False, require_authorized=False)
            next_config["target_operation_target_id"] = target.id
            next_config["target_group_id"] = group.id
            next_config["target_group_name"] = target.title or group.title
    elif task_type == "group_relay":
        normalized_sources: list[dict[str, Any]] = []
        for item in next_config.get("source_groups") or []:
            source = dict(item)
            _normalize_inline_target_input(session, tenant_id, source, target_type="group", id_field="operation_target_id", title_field="group_name")
            target_id = _as_int(source.get("operation_target_id"))
            if target_id:
                target, group = _group_for_operation_target(session, tenant_id, target_id, require_can_send=False, require_authorized=False)
                source["operation_target_id"] = target.id
                source["group_id"] = group.id
                source["group_name"] = source.get("group_name") or target.title or group.title
            normalized_sources.append(source)
        next_config["source_groups"] = normalized_sources

        _normalize_inline_target_input(session, tenant_id, next_config, target_type="group")
        target_id = _as_int(next_config.get("target_operation_target_id"))
        target_group_ids = _as_int_list(next_config.get("target_group_ids"))
        target_operation_target_ids = _as_int_list(next_config.get("target_operation_target_ids"))
        if target_id and target_id not in target_operation_target_ids:
            target_operation_target_ids.insert(0, target_id)
        resolved_target_group_ids: list[int] = []
        for operation_target_id in target_operation_target_ids:
            target, group = _group_for_operation_target(session, tenant_id, operation_target_id, require_can_send=False, require_authorized=False)
            resolved_target_group_ids.append(group.id)
        if resolved_target_group_ids:
            next_config["target_operation_target_ids"] = target_operation_target_ids
            next_config["target_operation_target_id"] = target_operation_target_ids[0]
            next_config["target_group_id"] = resolved_target_group_ids[0]
            target_group_ids = [*resolved_target_group_ids, *target_group_ids]
        if target_group_ids:
            next_config["target_group_ids"] = list(dict.fromkeys(target_group_ids))
    elif task_type in {"channel_view", "channel_like", "channel_comment"}:
        _normalize_inline_target_input(session, tenant_id, next_config, target_type="channel", id_field="target_channel_id", title_field="target_channel_name")
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
    data = _normalize_legacy_group_ai_config(task_type, data or {})
    normalized = model(**(data or {})).model_dump(mode="json", exclude_none=True)
    if task_type == "group_ai_chat":
        for field in GROUP_AI_LEGACY_RUNTIME_FIELDS:
            normalized.pop(field, None)
    for field in CHANNEL_JITTER_FIELDS.get(task_type, set()):
        normalized.pop(field, None)
    if task_type in {"group_relay", "channel_comment"}:
        normalized["require_review"] = False
    return normalized


def _normalize_legacy_group_ai_config(task_type: str, data: dict[str, Any]) -> dict[str, Any]:
    if task_type != "group_ai_chat" or "messages_per_round_mode" in data or "messages_per_round" not in data:
        return data
    return {**data, "messages_per_round_mode": "manual"}


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


def _group_for_operation_target(session: Session, tenant_id: int, target_id: int, *, require_can_send: bool, require_authorized: bool = True) -> tuple[OperationTarget, TgGroup]:
    target = session.get(OperationTarget, target_id)
    if not target or target.tenant_id != tenant_id or target.target_type != "group":
        raise ValueError("运营目标不存在")
    if require_authorized and target.auth_status != GroupAuthStatus.AUTHORIZED.value:
        raise ValueError("运营目标未授权")
    if require_can_send and not target.can_send:
        raise ValueError("运营目标不可发送")
    target = _canonical_group_target(session, tenant_id, target)
    group = session.scalar(
        select(TgGroup).where(
            TgGroup.tenant_id == tenant_id,
            TgGroup.tg_peer_id == target.tg_peer_id,
        )
    )
    if not group:
        group = TgGroup(
            tenant_id=tenant_id,
            tg_peer_id=target.tg_peer_id,
            title=target.title,
            group_type="supergroup",
            member_count=target.member_count,
            auth_status=target.auth_status,
            can_send=target.can_send,
        )
        session.add(group)
        session.flush()
    return target, group


def _canonical_group_target(session: Session, tenant_id: int, target: OperationTarget) -> OperationTarget:
    if _has_stable_group_reference(target):
        return target
    title = str(target.title or "").strip()
    if not title:
        return target
    candidates = list(
        session.scalars(
            select(OperationTarget)
            .where(
                OperationTarget.tenant_id == tenant_id,
                OperationTarget.target_type == "group",
                OperationTarget.id != target.id,
                OperationTarget.title == title,
                OperationTarget.can_send.is_(True),
                OperationTarget.auth_status == GroupAuthStatus.AUTHORIZED.value,
            )
            .order_by(OperationTarget.updated_at.desc(), OperationTarget.id.desc())
        )
    )
    stable_candidates = [candidate for candidate in candidates if _has_stable_group_reference(candidate)]
    return stable_candidates[0] if stable_candidates else target


def _has_stable_group_reference(target: OperationTarget) -> bool:
    return _is_stable_telegram_peer(target.tg_peer_id) or _looks_like_join_link(target.tg_peer_id)


def _is_stable_telegram_peer(peer_id: str) -> bool:
    value = str(peer_id or "").strip()
    return value.lstrip("-").isdigit()


def _looks_like_join_link(peer_id: str) -> bool:
    value = str(peer_id or "").strip()
    prefixes = ("+", "https://t.me/+", "http://t.me/+", "t.me/+", "https://telegram.me/+", "telegram.me/+")
    return value.startswith(prefixes)


def _normalize_inline_target_input(
    session: Session,
    tenant_id: int,
    config: dict[str, Any],
    *,
    target_type: str,
    id_field: str = "target_operation_target_id",
    title_field: str = "target_group_name",
) -> None:
    if _as_int(config.get(id_field)):
        return
    raw_input = str(config.get("target_input") or "").strip()
    if not raw_input:
        return
    title = str(config.get("target_title") or config.get(title_field) or raw_input).strip()
    target = _upsert_operation_target_from_input(session, tenant_id, target_type=target_type, raw_input=raw_input, title=title)
    config[id_field] = target.id
    config[title_field] = config.get(title_field) or target.title


def _upsert_operation_target_from_input(session: Session, tenant_id: int, *, target_type: str, raw_input: str, title: str) -> OperationTarget:
    normalized = _normalize_target_input(raw_input, target_type=target_type)
    target = session.scalar(
        select(OperationTarget).where(
            OperationTarget.tenant_id == tenant_id,
            OperationTarget.tg_peer_id == normalized["tg_peer_id"],
        )
    )
    if target:
        if target.target_type != target_type:
            raise ValueError(f"运营目标 {normalized['tg_peer_id']} 已存在为 {target.target_type}，不能作为 {target_type} 使用")
        if not target.username and normalized["username"]:
            target.username = normalized["username"]
        if title and target.title in {"", target.tg_peer_id}:
            target.title = title
        target.updated_at = _now()
        return target
    target = OperationTarget(
        tenant_id=tenant_id,
        target_type=target_type,
        tg_peer_id=normalized["tg_peer_id"],
        title=title or normalized["title"],
        username=normalized["username"],
        can_send=False,
        auth_status=GroupAuthStatus.UNVERIFIED.value,
    )
    session.add(target)
    session.flush()
    if target_type == "group":
        session.add(
            TgGroup(
                tenant_id=tenant_id,
                tg_peer_id=target.tg_peer_id,
                title=target.title,
                group_type="supergroup",
                member_count=0,
                auth_status=target.auth_status,
                can_send=False,
            )
        )
        session.flush()
    return target


def _normalize_target_input(raw_input: str, *, target_type: str) -> dict[str, str]:
    raw = raw_input.strip()
    username = ""
    normalized_peer = raw
    for prefix in ("https://t.me/", "http://t.me/", "t.me/", "https://telegram.me/", "http://telegram.me/", "telegram.me/"):
        if raw.startswith(prefix):
            tail = raw.split(prefix, 1)[1].split("?", 1)[0].strip("/")
            if tail and not tail.startswith(("+", "joinchat/")):
                username = tail
                normalized_peer = tail
            else:
                normalized_peer = f"{prefix}{tail}" if tail else raw.split("?", 1)[0].strip("/")
            break
    else:
        if raw.startswith("@"):
            username = raw.lstrip("@")
            normalized_peer = username
        elif raw.startswith("+"):
            normalized_peer = raw.split("?", 1)[0].strip("/")
    return {"tg_peer_id": normalized_peer, "username": username, "title": username or raw}


__all__ = [
    "apply_default_slang_config",
    "normalize_operation_target_references",
    "pacing_config_payload",
    "validate_rule_binding",
    "validated_type_config",
]

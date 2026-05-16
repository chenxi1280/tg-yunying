from __future__ import annotations

from typing import Any, Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import ChannelMessage, GroupAuthStatus, OperationTarget, RuleSet, RuleSetVersion, TgAccount
from app.schemas.risk_control import RiskPreflightRequest
from app.schemas.task_center import TaskPrecheckRequest
from app.services.risk_control import risk_preflight

from .account_pool import select_task_accounts
from .config_fields import COMMON_CREATE_FIELDS, TASK_CREATE_MODELS
from .utils import as_int as _as_int, as_int_list as _as_int_list, as_str_list as _as_str_list


NormalizeConfig = Callable[[Session, int, str, dict[str, Any]], dict[str, Any]]
ValidateTypeConfig = Callable[[str, dict[str, Any]], dict[str, Any]]
ValidateRuleBinding = Callable[[Session, int, dict[str, Any]], None]


def run_precheck_task_creation(
    session: Session,
    tenant_id: int,
    payload: TaskPrecheckRequest,
    *,
    normalize_operation_target_references: NormalizeConfig,
    validated_type_config: ValidateTypeConfig,
    validate_rule_binding: ValidateRuleBinding,
) -> dict[str, Any]:
    task_type = payload.task_type
    model = TASK_CREATE_MODELS.get(task_type)
    if model is None:
        raise ValueError(f"unknown task type: {task_type}")
    trace_id = ""
    warnings: list[str] = []
    blockers: list[str] = []
    risk_hits: list[str] = []
    suggested_actions: list[str] = []
    rule_version: dict[str, Any] | None = None
    target_ability: list[dict[str, Any]] = []
    estimated_actions = 0
    capacity_shortfall = 0
    try:
        create_payload = model(**(payload.payload or {}))
        raw_config = create_payload.model_dump(mode="json", exclude=COMMON_CREATE_FIELDS, exclude_unset=True)
        normalized_config = normalize_operation_target_references(session, tenant_id, task_type, raw_config)
        type_config = validated_type_config(task_type, normalized_config)
        validate_rule_binding(session, tenant_id, type_config)
        rule_version = _precheck_rule_version(session, tenant_id, type_config)
        target_ability, target_ids, target_blockers = _precheck_target_ability(session, tenant_id, task_type, type_config)
        blockers.extend(target_blockers)
        estimated_actions, target_per_unit = _precheck_estimated_actions(session, tenant_id, task_type, type_config)
    except ValueError as exc:
        blockers.append(str(exc))
        create_payload = None
        target_ids = []
        target_per_unit = 1

    account_config = create_payload.account_config.model_dump(mode="json") if create_payload else dict((payload.payload or {}).get("account_config") or {})
    candidates = _precheck_candidate_accounts(session, tenant_id, account_config)
    available_accounts = select_task_accounts(session, tenant_id, account_config, limit=max(len(candidates), 1)) if candidates else []
    if candidates:
        risk_payload = RiskPreflightRequest(
            scenario="task_create",
            task_type=task_type,
            account_ids=[account.id for account in candidates],
            target_ids=target_ids,
            content_preview=_precheck_content_preview(task_type, payload.payload or {}),
            scheduled_at=create_payload.scheduled_start if create_payload else None,
        )
        risk = risk_preflight(session, tenant_id, risk_payload)
    else:
        risk = {"decision": "block", "decision_reasons": ["no_available_account"], "available_accounts": [], "limited_accounts": [], "blocked_accounts": [], "target_warnings": [], "content_warnings": [], "proxy_warnings": [], "suggested_actions": [], "trace_id": ""}
    trace_id = str(risk.get("trace_id") or "")
    risk_hits = [*_as_str_list(risk.get("decision_reasons")), *_as_str_list(risk.get("target_warnings")), *_as_str_list(risk.get("content_warnings")), *_as_str_list(risk.get("proxy_warnings"))]
    suggested_actions.extend(_as_str_list(risk.get("suggested_actions")))
    available_count = min(len(available_accounts), len(risk.get("available_accounts") or available_accounts))
    limited_count = len(risk.get("limited_accounts") or [])
    blocked_count = len(risk.get("blocked_accounts") or [])
    if estimated_actions and target_per_unit:
        required_parallel = min(max(estimated_actions, 1), max(int(target_per_unit), 1))
        capacity_shortfall = max(0, required_parallel - available_count)
    if capacity_shortfall:
        warnings.append(f"预计单轮需要 {max(int(target_per_unit), 1)} 个账号，当前可用 {available_count} 个")
    if risk.get("decision") == "block":
        blockers.extend(_as_str_list(risk.get("decision_reasons")) or ["风控预检阻塞"])
    elif risk.get("decision") == "warn":
        warnings.extend(_as_str_list(risk.get("decision_reasons")))
    if not candidates:
        blockers.append("没有匹配账号")
    decision = "block" if blockers else "warn" if warnings or risk_hits or capacity_shortfall else "allow"
    return {
        "task_type": task_type,
        "decision": decision,
        "available_account_count": available_count,
        "candidate_account_count": len(candidates),
        "limited_account_count": limited_count,
        "blocked_account_count": blocked_count,
        "target_ability": target_ability,
        "estimated_actions": estimated_actions,
        "capacity_shortfall": capacity_shortfall,
        "rule_version": rule_version,
        "risk_hits": sorted(set(filter(None, risk_hits))),
        "blockers": sorted(set(filter(None, blockers))),
        "warnings": sorted(set(filter(None, warnings))),
        "suggested_actions": sorted(set(filter(None, suggested_actions))),
        "trace_id": trace_id,
    }


def _precheck_candidate_accounts(session: Session, tenant_id: int, account_config: dict[str, Any]) -> list[TgAccount]:
    stmt = select(TgAccount).where(TgAccount.tenant_id == tenant_id, TgAccount.deleted_at.is_(None)).order_by(TgAccount.health_score.desc(), TgAccount.id.asc())
    mode = account_config.get("selection_mode") or "all"
    if mode == "manual":
        account_ids = _as_int_list(account_config.get("account_ids"))
        if not account_ids:
            return []
        stmt = stmt.where(TgAccount.id.in_(account_ids))
    elif mode == "group":
        pool_id = _as_int(account_config.get("account_group_id"))
        if not pool_id:
            return []
        stmt = stmt.where(TgAccount.pool_id == pool_id)
    return list(session.scalars(stmt))


def _precheck_rule_version(session: Session, tenant_id: int, config: dict[str, Any]) -> dict[str, Any] | None:
    version_id = _as_int(config.get("rule_set_version_id"))
    rule_set_id = _as_int(config.get("rule_set_id"))
    version = session.get(RuleSetVersion, version_id) if version_id else None
    if not version and rule_set_id:
        rule_set = session.get(RuleSet, rule_set_id)
        version = session.get(RuleSetVersion, rule_set.active_version_id) if rule_set and rule_set.active_version_id else None
    if not version or version.tenant_id != tenant_id:
        return None
    return {"id": version.id, "rule_set_id": version.rule_set_id, "version": version.version, "status": version.status}


def _precheck_target_ability(session: Session, tenant_id: int, task_type: str, config: dict[str, Any]) -> tuple[list[dict[str, Any]], list[int], list[str]]:
    refs = _precheck_target_refs(task_type, config)
    target_ids = list(dict.fromkeys([target_id for target_id, _role, _require_send in refs]))
    abilities: list[dict[str, Any]] = []
    blockers: list[str] = []
    for target_id, role, require_send in refs:
        target = session.get(OperationTarget, target_id)
        if not target or target.tenant_id != tenant_id:
            blockers.append(f"运营目标 #{target_id} 不存在")
            continue
        authorized = target.auth_status == GroupAuthStatus.AUTHORIZED.value
        can_task = bool(authorized and (target.can_send or not require_send))
        if not can_task:
            blockers.append(f"{target.title} 当前不可作为{'发送目标' if require_send else '监听来源'}创建任务")
        abilities.append({
            "target_id": target.id,
            "title": target.title,
            "target_type": target.target_type,
            "role": role,
            "can_send": bool(target.can_send),
            "auth_status": target.auth_status,
            "can_task": can_task,
            "member_count": target.member_count,
        })
    return abilities, target_ids, blockers


def _precheck_target_refs(task_type: str, config: dict[str, Any]) -> list[tuple[int, str, bool]]:
    if task_type == "group_ai_chat":
        return [(target_id, "send_target", True) for target_id in _as_int_list(config.get("target_operation_target_id"))]
    if task_type == "group_relay":
        refs: list[tuple[int, str, bool]] = []
        refs.extend((target_id, "send_target", True) for target_id in _as_int_list(config.get("target_operation_target_ids")))
        refs.extend((target_id, "send_target", True) for target_id in _as_int_list(config.get("target_operation_target_id")))
        refs.extend((source_id, "listen_source", False) for source_id in [_as_int(item.get("operation_target_id")) for item in config.get("source_groups") or [] if isinstance(item, dict)] if source_id)
        return list(dict.fromkeys(refs))
    return [(target_id, "send_target", True) for target_id in _as_int_list(config.get("target_channel_id"))]


def _precheck_estimated_actions(session: Session, tenant_id: int, task_type: str, config: dict[str, Any]) -> tuple[int, int]:
    if task_type == "group_ai_chat":
        count = int(config.get("messages_per_round") or 1) if config.get("messages_per_round_mode") == "manual" else 3
        return count, count
    if task_type == "group_relay":
        source_count = max(1, len(config.get("source_groups") or []))
        target_count = max(1, len(_as_int_list(config.get("target_operation_target_ids")) or _as_int_list(config.get("target_group_ids"))))
        return source_count * target_count, target_count
    message_count = _precheck_channel_message_count(session, tenant_id, config)
    if task_type == "channel_view":
        per_message = int(config.get("target_views_per_message") or 1)
    elif task_type == "channel_like":
        per_message = int(config.get("target_likes_per_message") or 1)
    else:
        per_message = int(config.get("target_comments_per_message") or 1)
    return message_count * per_message, per_message


def _precheck_channel_message_count(session: Session, tenant_id: int, config: dict[str, Any]) -> int:
    scope = config.get("message_scope") or "latest_n"
    if scope == "specific":
        return len(config.get("message_ids") or [])
    if scope == "latest_n":
        return int(config.get("message_count") or 1)
    target_id = _as_int(config.get("target_channel_id"))
    stmt = select(func.count(ChannelMessage.id)).where(ChannelMessage.tenant_id == tenant_id)
    if target_id:
        stmt = stmt.where(ChannelMessage.channel_target_id == target_id)
    if scope == "date_range":
        if config.get("date_from"):
            stmt = stmt.where(ChannelMessage.published_at >= config["date_from"])
        if config.get("date_to"):
            stmt = stmt.where(ChannelMessage.published_at <= config["date_to"])
    count = int(session.scalar(stmt) or 0)
    return max(1, count)


def _precheck_content_preview(task_type: str, payload: dict[str, Any]) -> str:
    if task_type == "group_ai_chat":
        return str(payload.get("topic_hint") or payload.get("system_prompt_override") or "")
    if task_type == "group_relay":
        return str(payload.get("content_mode") or "")
    return str(payload.get("topic_hint") or payload.get("comment_style") or payload.get("target_channel_name") or "")

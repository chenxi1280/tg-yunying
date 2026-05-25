from __future__ import annotations

from typing import Any, Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import ChannelMessage, GroupAuthStatus, OperationTarget, RuleSet, RuleSetVersion, TgAccount
from app.schemas.risk_control import RiskPreflightRequest
from app.schemas.task_center import TaskPrecheckRequest
from app.services.risk_control import risk_preflight

from .account_pool import select_task_accounts
from .channel_membership import channel_membership_summary
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
    target_resolution: dict[str, Any] = {}
    membership_summary: dict[str, Any] = {}
    capacity_summary: dict[str, Any] = {}
    estimated_actions = 0
    capacity_shortfall = 0
    try:
        create_payload = model(**(payload.payload or {}))
        raw_config = create_payload.model_dump(mode="json", exclude=COMMON_CREATE_FIELDS, exclude_unset=True)
        normalized_config = normalize_operation_target_references(session, tenant_id, task_type, raw_config)
        type_config = validated_type_config(task_type, normalized_config)
        target_resolution = _precheck_target_resolution(session, tenant_id, task_type, raw_config, type_config)
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
    if task_type in {"channel_view", "channel_like", "channel_comment", "group_ai_chat", "group_relay"} and target_ability:
        membership_summary = _precheck_membership_summary(session, tenant_id, target_ability, account_config, candidates)
    membership_subtask_preview = _membership_subtask_preview(membership_summary)
    available_accounts = (
        select_task_accounts(
            session,
            tenant_id,
            account_config,
            limit=max(len(candidates), 1),
            enforce_max_concurrent=False,
        )
        if candidates
        else []
    )
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
    capacity_summary = _precheck_capacity_summary(
        target_per_unit=target_per_unit,
        candidate_count=len(candidates),
        effective_count=available_count,
        max_concurrent=int(account_config.get("max_concurrent") or 20),
        shortfall=capacity_shortfall,
    )
    if capacity_shortfall:
        warnings.append(f"预计单轮需要 {max(int(target_per_unit), 1)} 个账号，当前可用 {available_count} 个")
    if membership_summary:
        need_join = int(membership_summary.get("need_join_account_count") or 0)
        joined = int(membership_summary.get("joined_account_count") or 0)
        failed = int(membership_summary.get("failed_account_count") or 0)
        if need_join:
            warnings.append(f"目标准入前置：已满足 {joined} 个，需准备 {need_join} 个")
        if failed:
            warnings.append(f"目标准入前置：已有 {failed} 个账号准备失败")
    if risk.get("decision") == "block":
        decision_reasons = _as_str_list(risk.get("decision_reasons")) or ["风控预检阻塞"]
        if set(decision_reasons) <= {"target_warning"} and target_ability and all(bool(item.get("can_task")) for item in target_ability):
            warnings.extend(decision_reasons)
        else:
            blockers.extend(decision_reasons)
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
        "target_resolution": target_resolution,
        "membership_summary": membership_summary,
        "ready_account_count": int(membership_summary.get("joined_account_count") or 0),
        "preparable_account_count": int(membership_summary.get("need_join_account_count") or 0),
        "estimated_membership_actions": int(membership_summary.get("estimated_membership_actions") or 0),
        "membership_warnings": _membership_warnings(membership_summary),
        "membership_subtask_preview": membership_subtask_preview,
        "estimated_actions": estimated_actions,
        "capacity_shortfall": capacity_shortfall,
        "capacity_summary": capacity_summary,
        "rule_version": rule_version,
        "risk_hits": sorted(set(filter(None, risk_hits))),
        "blockers": sorted(set(filter(None, blockers))),
        "warnings": sorted(set(filter(None, warnings))),
        "suggested_actions": sorted(set(filter(None, suggested_actions))),
        "trace_id": trace_id,
    }


def _precheck_capacity_summary(
    *,
    target_per_unit: int,
    candidate_count: int,
    effective_count: int,
    max_concurrent: int,
    shortfall: int,
) -> dict[str, Any]:
    return {
        "target_per_message": max(int(target_per_unit or 0), 0),
        "candidate_account_count": max(int(candidate_count or 0), 0),
        "effective_account_count": max(int(effective_count or 0), 0),
        "max_concurrent": max(int(max_concurrent or 0), 0),
        "capacity_shortfall": max(int(shortfall or 0), 0),
        "limit_note": "max_concurrent 仅控制同时执行数量，不截断本轮可参与账号池",
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
        is_channel_task = task_type in {"channel_view", "channel_like", "channel_comment"} and target.target_type == "channel"
        has_join_entry = bool(target.username or str(target.tg_peer_id).startswith(("https://t.me/", "http://t.me/", "t.me/", "https://telegram.me/", "http://telegram.me/", "telegram.me/", "+")))
        preparable_group = target.target_type == "group" and has_join_entry
        authorized = target.auth_status == GroupAuthStatus.AUTHORIZED.value or is_channel_task or preparable_group
        can_task = bool(authorized and (target.can_send or not require_send or is_channel_task or preparable_group))
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
            "preparable": preparable_group or is_channel_task,
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


def _precheck_target_resolution(session: Session, tenant_id: int, task_type: str, raw_config: dict[str, Any], type_config: dict[str, Any]) -> dict[str, Any]:
    if task_type == "group_relay":
        source_items: list[dict[str, Any]] = []
        for source in raw_config.get("source_groups") or []:
            if not isinstance(source, dict):
                continue
            source_id = _as_int(source.get("operation_target_id"))
            if not source_id:
                source_id = _matching_source_operation_target_id(source, type_config)
            source_items.append(_target_resolution_item(session, tenant_id, source_id, source, role="listen_source"))
        target_items: list[dict[str, Any]] = []
        target_ids = _as_int_list(type_config.get("target_operation_target_ids"))
        if not target_ids and _as_int(type_config.get("target_operation_target_id")):
            target_ids = [_as_int(type_config.get("target_operation_target_id"))]
        for target_id in target_ids:
            target_items.append(_target_resolution_item(session, tenant_id, target_id, raw_config, role="send_target"))
        first = (target_items or source_items or [{}])[0]
        unresolved = [item for item in [*source_items, *target_items] if item.get("status") == "unresolved"]
        return {
            "status": "unresolved" if unresolved else "created_or_reused" if any((item.get("target_input") for item in [*source_items, *target_items])) else "reused",
            "target_id": first.get("target_id"),
            "target_type": first.get("target_type") or "group",
            "target_input": raw_config.get("target_input") or "",
            "title": first.get("title") or "",
            "username": first.get("username") or "",
            "tg_peer_id": first.get("tg_peer_id") or "",
            "missing_join_entry": any(bool(item.get("missing_join_entry")) for item in [*source_items, *target_items]),
            "sources": source_items,
            "targets": target_items,
        }
    target_id = 0
    if task_type in {"channel_view", "channel_like", "channel_comment"}:
        target_id = _as_int(type_config.get("target_channel_id"))
    elif task_type == "group_ai_chat":
        target_id = _as_int(type_config.get("target_operation_target_id"))
    elif task_type == "group_relay":
        target_id = _as_int(type_config.get("target_operation_target_id")) or (_as_int_list(type_config.get("target_operation_target_ids")) or [0])[0]
    return _target_resolution_item(session, tenant_id, target_id, raw_config, role="send_target")


def _target_resolution_item(session: Session, tenant_id: int, target_id: int, raw_config: dict[str, Any], *, role: str) -> dict[str, Any]:
    target = session.get(OperationTarget, target_id) if target_id else None
    target_input = str(raw_config.get("target_input") or "").strip()
    status = "reused" if target and not target_input else "created_or_reused" if target else "unresolved"
    return {
        "role": role,
        "status": status,
        "target_id": target.id if target else None,
        "target_type": target.target_type if target else raw_config.get("target_type") or "group",
        "target_input": target_input,
        "title": target.title if target else raw_config.get("target_title") or raw_config.get("group_name") or "",
        "username": target.username if target else "",
        "tg_peer_id": target.tg_peer_id if target else "",
        "missing_join_entry": bool(target and not (target.username or str(target.tg_peer_id).startswith(("https://t.me/", "http://t.me/", "t.me/", "+")))),
    }


def _matching_source_operation_target_id(source: dict[str, Any], type_config: dict[str, Any]) -> int:
    source_input = str(source.get("target_input") or "").strip()
    source_name = str(source.get("group_name") or source.get("target_title") or "").strip()
    for item in type_config.get("source_groups") or []:
        if not isinstance(item, dict):
            continue
        if source_input and source_input == str(item.get("target_input") or "").strip():
            return _as_int(item.get("operation_target_id"))
        if source_name and source_name == str(item.get("group_name") or "").strip():
            return _as_int(item.get("operation_target_id"))
    return 0


def _precheck_membership_summary(
    session: Session,
    tenant_id: int,
    target_ability: list[dict[str, Any]],
    account_config: dict[str, Any],
    candidates: list[TgAccount],
) -> dict[str, Any]:
    summaries: list[dict[str, Any]] = []
    for ability in target_ability:
        target_id = _as_int(ability.get("target_id"))
        target = session.get(OperationTarget, target_id) if target_id else None
        if not target or target.tenant_id != tenant_id or target.target_type not in {"channel", "group"}:
            continue
        require_send = target.target_type == "group" and ability.get("role") == "send_target"
        summary = channel_membership_summary(session, tenant_id, target, account_config, candidates=candidates, require_send=require_send)
        summary["target_id"] = target.id
        summary["title"] = target.title
        summary["role"] = ability.get("role") or ""
        summary["target_resolve_status"] = target.auth_status
        summary["estimated_duration_seconds_min"] = 0 if not summary.get("need_join_account_count") else 30
        summary["estimated_duration_seconds_max"] = int(summary.get("need_join_account_count") or 0) * 180
        summary["effective_interaction_account_count"] = int(summary.get("joined_account_count") or 0) + int(summary.get("need_join_account_count") or 0)
        ability["membership"] = summary
        summaries.append(summary)
    if not summaries:
        return {}
    if len(summaries) == 1:
        return summaries[0]
    return {
        "target_type": "mixed",
        "subtask_type": "ensure_target_membership",
        "target_count": len(summaries),
        "candidate_account_count": len(candidates),
        "joined_account_count": sum(int(item.get("joined_account_count") or 0) for item in summaries),
        "need_join_account_count": sum(int(item.get("need_join_account_count") or 0) for item in summaries),
        "failed_account_count": sum(int(item.get("failed_account_count") or 0) for item in summaries),
        "blocked_account_count": sum(int(item.get("blocked_account_count") or 0) for item in summaries),
        "estimated_membership_actions": sum(int(item.get("estimated_membership_actions") or 0) for item in summaries),
        "estimated_duration_seconds_min": min(int(item.get("estimated_duration_seconds_min") or 0) for item in summaries),
        "estimated_duration_seconds_max": sum(int(item.get("estimated_duration_seconds_max") or 0) for item in summaries),
        "effective_interaction_account_count": sum(int(item.get("effective_interaction_account_count") or 0) for item in summaries),
        "targets": summaries,
    }


def _membership_warnings(summary: dict[str, Any]) -> list[str]:
    if not summary:
        return []
    warnings: list[str] = []
    if int(summary.get("need_join_account_count") or 0):
        warnings.append("部分账号需要先完成关注或加入")
    if int(summary.get("failed_account_count") or 0):
        warnings.append("已有账号准入失败，可在详情中查看原因")
    return warnings


def _membership_subtask_preview(summary: dict[str, Any]) -> dict[str, Any]:
    if not summary:
        return {}
    pending = int(summary.get("need_join_account_count") or 0)
    ready = int(summary.get("joined_account_count") or 0)
    failed = int(summary.get("failed_account_count") or 0)
    total = max(ready + pending + failed, 1)
    return {
        "subtask_type": "target_membership",
        "status": "not_required" if pending == 0 and failed == 0 else "pending" if pending else "partial_success" if ready else "blocked",
        "progress_percent": round((ready + failed) * 100 / total),
        "estimated_remaining_seconds": pending * 180,
        "ready_account_count": ready,
        "pending_account_count": pending,
        "failed_account_count": failed,
        "blocked_account_count": int(summary.get("blocked_account_count") or 0),
        "warnings": _membership_warnings(summary),
    }


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
        per_message = int(config.get("per_message_daily_view_target") or config.get("target_views_per_message") or 1)
        task_cap = int(config.get("task_daily_view_safety_cap") or 0)
        estimated = message_count * per_message
        if task_cap > 0:
            estimated = min(estimated, task_cap)
        return estimated, per_message
    elif task_type == "channel_like":
        per_message = int(config.get("target_likes_per_message") or 1)
    else:
        per_message = int(config.get("target_comments_per_message") or 1)
    return message_count * per_message, per_message


def _precheck_channel_message_count(session: Session, tenant_id: int, config: dict[str, Any]) -> int:
    scope = config.get("initial_message_scope") or config.get("message_scope") or "latest_n"
    if scope == "new_only":
        return 0
    if scope == "specific":
        return len(config.get("message_ids") or [])
    if scope == "latest_n":
        return int(config.get("latest_message_count") or config.get("message_count") or 1)
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

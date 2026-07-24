from __future__ import annotations

import hashlib
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AccountStatus, GroupAuthStatus, OperationTarget, RuleSet, RuleSetVersion, SourceMediaAsset, Task, TgAccount, TgGroup, TgGroupAccount
from app.services.account_capacity import AccountCapacityCache, available_accounts_by_capacity, next_capacity_window
from app.services.content_filters import filter_outbound_content
from app.services.group_listeners import collect_group_context, is_listener_ignored_sender, recent_context_messages
from app.services.rule_engine import apply_output_policy, bound_rule_version
from app.services.material_rules import select_material_for_policy

from ..account_pool import select_task_accounts
from ..ai_generator import rewrite_relay_content
from ..fingerprints import is_duplicate, remember_fingerprint
from ..listener_runtime import should_collect_listener
from ..pacing import schedule_times
from ..payloads import SendMessagePayload, create_send_action
from app.services.source_media import SOURCE_MEDIA_READY, ready_media_segments, register_action_waiting_for_source_media
from ..channel_membership import gate_channel_membership, linked_channel_group
from ..targets import group_from_reference
from .common import add_tokens, stats_inc


def build_plan(session: Session, task: Task) -> int:
    if not bound_rule_version(session, task):
        return 0
    config = effective_relay_config(session, task)
    account_cache: dict[int, list[Any]] = {}
    candidate_actions: list[dict[str, Any]] = []
    membership_actions_created = 0
    monitor_account_ids = [int(account_id) for account_id in config.get("monitor_account_ids") or []]
    for item in [item for item in config.get("source_groups") or [] if item.get("is_active", True)]:
        source_target = _operation_target_from_id(session, task.tenant_id, item.get("operation_target_id"))
        if source_target and source_target.target_type == "group" and not item.get("group_id"):
            gate = gate_channel_membership(session, task, source_target, require_send=False)
            membership_actions_created += gate.created
            if not gate.ready:
                continue
        source = group_from_reference(
            session,
            task.tenant_id,
            group_id=int(item.get("group_id") or 0) or None,
            operation_target_id=int(item.get("operation_target_id") or 0) or None,
            require_authorized=False if source_target else True,
        )
        if not source:
            continue
        source_operation_target_id = int(item.get("operation_target_id") or 0) or _source_operation_target_id(config, source.id)
        if should_collect_listener("group", source.id, window_seconds=source.listener_interval_seconds):
            collect_group_context(session, source, _source_monitor_account_ids(session, task, source, monitor_account_ids), create_source_media=True, learning_scene=None)
        for message in reversed(recent_context_messages(session, source, source.listener_context_limit)):
            if is_listener_ignored_sender(session, source, message):
                continue
            source_filter_reason = relay_source_filter_reason(message, config)
            if source_filter_reason:
                continue
            if not passes_relay_filters(message.content, message.sender_peer_id, message.message_type, config.get("filters") or {}):
                continue
            targets, target_membership_created = _relay_targets_with_membership(session, task, config, source.id, message.content)
            membership_actions_created += target_membership_created
            if not targets:
                task.last_error = "目标群不存在或未授权"
                continue
            for target in targets:
                source_fingerprint_key = f"{task.id}:relay:{source.id}:target:{target.id}"
                if is_duplicate(session, task.tenant_id, source_fingerprint_key, message.content, window_minutes=int(config.get("dedup_window_minutes") or 60)):
                    continue
                accounts = account_cache.get(target.id)
                if accounts is None:
                    accounts = _select_relay_accounts(session, task, config, target.id)
                    account_cache[target.id] = accounts
                if not accounts:
                    task.last_error = "没有可用账号，等待账号恢复后继续执行"
                    stats_inc(task, "failure_count")
                    continue
                rewritten, tokens = rewrite_relay_content(session, task.tenant_id, config, message.content, target_label=target.title)
                rewritten = apply_transform_rules(rewritten, config.get("transforms") or {})
                add_tokens(task, tokens)
                policy_result = apply_output_policy(rewritten, config.get("output_checks") or {}, config.get("transforms") or {})
                if not policy_result.allowed:
                    stats_inc(task, "failure_count")
                    remember_fingerprint(session, task.tenant_id, source_fingerprint_key, message.content)
                    continue
                rewritten = policy_result.content
                filtered = filter_outbound_content(session, tenant_id=task.tenant_id, group=target, content=rewritten, reject_mentions=True, reject_replies=True)
                if not filtered.ok:
                    stats_inc(task, "failure_count")
                    remember_fingerprint(session, task.tenant_id, source_fingerprint_key, message.content)
                    continue
                candidate_actions.append(
                    {
                        "target": target,
                        "source_id": source.id,
                        "source_group_title": source.title,
                        "source_operation_target_id": source_operation_target_id,
                        "target_operation_target": _operation_target_for_group(session, task.tenant_id, target),
                        "original": message.content,
                        "content": filtered.content,
                        "source_info": f"{source.title} / {message.sender_name}",
                        "source_sender_name": message.sender_name,
                        "source_sender_peer_id": message.sender_peer_id,
                        "source_sender_username": getattr(message, "sender_username", "") or "",
                        "source_sender_role": getattr(message, "sender_role", "") or "",
                        "source_is_bot": bool(getattr(message, "is_bot", False)),
                        "source_filter_reason": source_filter_reason,
                        "source_remote_message_id": message.remote_message_id,
                        "source_message_type": message.message_type,
                        "source_sent_at": message.sent_at,
                        "source_media_asset_ids": [
                            asset.id
                            for asset in _source_media_assets_for_message(session, task.tenant_id, source.id, message.remote_message_id)
                        ] if config.get("preserve_media") and message.message_type != "text" else [],
                    }
                )
                remember_fingerprint(session, task.tenant_id, source_fingerprint_key, message.content)
    if not candidate_actions:
        return membership_actions_created
    times = schedule_times(len(candidate_actions), task.pacing_config or {})
    capacity_cache = AccountCapacityCache()
    batch_index = int((task.stats or {}).get("total_rounds") or 0) + 1
    relay_batch_id = f"{task.id}:batch:{batch_index}"
    created = 0
    target_offsets: dict[str, int] = {}
    for index, candidate in enumerate(candidate_actions):
        target = candidate["target"]
        source_id = int(candidate["source_id"])
        source_operation_target_id = candidate.get("source_operation_target_id")
        target_operation_target = candidate.get("target_operation_target")
        target_operation_target_id = target_operation_target.id if target_operation_target else None
        original = str(candidate.get("original") or "")
        content = str(candidate.get("content") or "")
        accounts = account_cache.get(target.id) or []
        if not accounts:
            stats_inc(task, "failure_count")
            continue
        planned_at = times[index]
        available_accounts = available_accounts_by_capacity(
            session,
            tenant_id=task.tenant_id,
            accounts=accounts,
            scheduled_at=planned_at,
            cache=capacity_cache,
        )
        account_pool = available_accounts or accounts
        account = _pick_relay_account(account_pool, target.id, source_id, original, config, target_offsets)
        if not available_accounts:
            decision = next_capacity_window(
                session,
                tenant_id=task.tenant_id,
                account_ids=[item.id for item in accounts],
                scheduled_at=planned_at,
                cache=capacity_cache,
            )
            if decision.defer_until:
                planned_at = decision.defer_until
        material_result = select_material_for_policy(
            session,
            task.tenant_id,
            (config.get("routing") or {}).get("material_policy") or config.get("material_policy") or {},
            context_key=f"{task.id}:{target.id}:{source_id}:{original}",
            default_caption="",
        )
        if material_result.failure_reason and material_result.fallback == "skip":
            stats_inc(task, "failure_count")
            continue
        material_segments = [material_result.segment] if material_result.ok and material_result.segment else []
        source_media_asset_ids = [str(asset_id) for asset_id in candidate.get("source_media_asset_ids") or []]
        if material_result.action in {"replace_media", "replace_source_media"} and material_segments:
            source_media_asset_ids = []
        action = create_send_action(
            session,
            task,
            account.id,
            planned_at,
            SendMessagePayload(
                chat_id=target.tg_peer_id,
                group_id=target.id,
                operation_target_id=target_operation_target_id,
                target_operation_target_id=target_operation_target_id,
                target_reference_revision=(
                    int(target_operation_target.reference_revision or 1)
                    if target_operation_target
                    else None
                ),
                target_reference_snapshot=(
                    {
                        "tg_peer_id": str(target_operation_target.tg_peer_id),
                        "username": str(target_operation_target.username or ""),
                        "title": str(target_operation_target.title),
                    }
                    if target_operation_target
                    else {}
                ),
                task_config_revision=int(task.config_revision or 1),
                target_display=target.title,
                message_text=content,
                original_text=original,
                review_approved=True,
                relay_batch_id=relay_batch_id,
                relay_event_id=f"event:{source_id}:{_content_hash(original)}",
                source_group_id=source_id,
                source_operation_target_id=source_operation_target_id,
                source_info=str(candidate.get("source_info") or ""),
                source_group_title=str(candidate.get("source_group_title") or ""),
                source_sender_name=str(candidate.get("source_sender_name") or ""),
                source_sender_peer_id=str(candidate.get("source_sender_peer_id") or ""),
                source_sender_username=str(candidate.get("source_sender_username") or ""),
                source_sender_role=str(candidate.get("source_sender_role") or ""),
                source_is_bot=bool(candidate.get("source_is_bot") or False),
                source_filter_reason=str(candidate.get("source_filter_reason") or ""),
                source_remote_message_id=str(candidate.get("source_remote_message_id") or ""),
                source_message_type=str(candidate.get("source_message_type") or ""),
                source_sent_at=candidate.get("source_sent_at"),
                source_media_asset_ids=source_media_asset_ids,
                media_segments=material_segments,
                rule_set_id=config.get("rule_set_id"),
                rule_set_name=str(config.get("rule_set_name") or ""),
                rule_set_version_id=config.get("rule_set_version_id"),
                resolved_rule_set_version_id=config.get("resolved_rule_set_version_id") or config.get("rule_set_version_id"),
                rule_set_version=config.get("rule_set_version"),
                rule_binding_mode=str(config.get("rule_binding_mode") or ""),
                rule_trace={
                    **_relay_rule_trace(config, source_id, target.id, original, content, account.id),
                    "material_policy": (config.get("routing") or {}).get("material_policy") or config.get("material_policy") or {},
                    "material_action": material_result.action,
                    "material_id": material_result.selected.id if material_result.selected else None,
                    "material_failure_reason": material_result.failure_reason,
                },
            ),
        )
        _attach_source_media_or_wait(session, action, source_media_asset_ids)
        created += 1
    stats_inc(task, "total_rounds")
    return created


def _source_media_assets_for_message(session: Session, tenant_id: int, source_group_id: int, remote_message_id: str) -> list[SourceMediaAsset]:
    if not remote_message_id:
        return []
    assets = list(
        session.scalars(
            select(SourceMediaAsset)
            .where(
                SourceMediaAsset.tenant_id == tenant_id,
                SourceMediaAsset.source_group_id == source_group_id,
                SourceMediaAsset.source_message_id == str(remote_message_id),
            )
            .order_by(SourceMediaAsset.source_media_group_id.asc(), SourceMediaAsset.media_group_index.asc(), SourceMediaAsset.created_at.asc())
        )
    )
    if not assets:
        return []
    group_id = assets[0].source_media_group_id
    if group_id:
        return list(
            session.scalars(
                select(SourceMediaAsset)
                .where(
                    SourceMediaAsset.tenant_id == tenant_id,
                    SourceMediaAsset.source_group_id == source_group_id,
                    SourceMediaAsset.source_media_group_id == group_id,
                )
                .order_by(SourceMediaAsset.media_group_index.asc(), SourceMediaAsset.created_at.asc())
            )
        )
    return assets


def _attach_source_media_or_wait(session: Session, action, asset_ids: list[str]) -> None:
    if not asset_ids:
        return
    assets = list(session.scalars(select(SourceMediaAsset).where(SourceMediaAsset.id.in_(asset_ids))))
    ready_count = sum(1 for asset in assets if asset.cache_status == SOURCE_MEDIA_READY and asset.cache_peer_id and asset.cache_message_id)
    payload = dict(action.payload or {})
    payload["source_media_asset_ids"] = asset_ids
    if ready_count:
        payload["media_segments"] = [*(payload.get("media_segments") or []), *ready_media_segments(session, asset_ids)]
        payload["album_segment_results"] = [
            {
                "source_media_asset_id": asset.id,
                "media_group_index": asset.media_group_index,
                "status": "ready" if asset.cache_status == SOURCE_MEDIA_READY else "album_segment_failed",
                "reason": "" if asset.cache_status == SOURCE_MEDIA_READY else (asset.failure_reason or asset.cache_status),
            }
            for asset in sorted(assets, key=lambda item: (item.source_media_group_id or item.source_message_id or "", item.media_group_index, item.created_at))
        ]
    action.payload = payload
    if ready_count < len(asset_ids):
        register_action_waiting_for_source_media(session, action, asset_ids)


def _source_monitor_account_ids(session: Session, task: Task, source: TgGroup, configured_ids: list[int]) -> list[int]:
    if configured_ids:
        return configured_ids
    return list(
        session.scalars(
            select(TgAccount.id)
            .join(TgGroupAccount, TgGroupAccount.account_id == TgAccount.id)
            .where(
                TgAccount.tenant_id == task.tenant_id,
                TgAccount.deleted_at.is_(None),
                TgAccount.status == AccountStatus.ACTIVE.value,
                TgGroupAccount.tenant_id == task.tenant_id,
                TgGroupAccount.group_id == source.id,
            )
            .order_by(TgGroupAccount.is_listener.desc(), TgAccount.health_score.desc(), TgAccount.id.asc())
        )
    )


def _source_operation_target_id(config: dict[str, Any], source_group_id: int) -> int | None:
    for item in config.get("source_groups") or []:
        if not isinstance(item, dict):
            continue
        try:
            if int(item.get("group_id") or 0) == source_group_id and item.get("operation_target_id"):
                return int(item["operation_target_id"])
        except (TypeError, ValueError):
            continue
    return None


def _operation_target_for_group(session: Session, tenant_id: int, group: TgGroup) -> OperationTarget | None:
    return session.scalar(
        select(OperationTarget)
        .where(
            OperationTarget.tenant_id == tenant_id,
            OperationTarget.target_type == "group",
            OperationTarget.tg_peer_id == group.tg_peer_id,
        )
        .order_by(OperationTarget.id.asc())
        .limit(1)
    )


def effective_relay_config(session: Session, task: Task) -> dict[str, Any]:
    config = dict(task.type_config or {})
    config = _with_source_filter_defaults(config)
    version = _bound_rule_version(session, task)
    if not version:
        return config
    rule_set = session.get(RuleSet, version.rule_set_id)
    transforms = dict(version.transforms or {})
    routing = dict(version.routing or {})
    account_strategy = dict(version.account_strategy or {})
    retry_policy = dict(version.retry_policy or {})
    config["rule_set_id"] = version.rule_set_id
    config["rule_set_name"] = rule_set.name if rule_set else ""
    config["rule_set_version_id"] = version.id
    config["resolved_rule_set_version_id"] = version.id
    config["rule_set_version"] = version.version
    config["rule_binding_mode"] = "fixed_version" if (task.type_config or {}).get("rule_set_version_id") else "follow_current"
    config["filters"] = dict(version.filters or {})
    config["output_checks"] = dict(version.output_checks or {})
    config["transforms"] = transforms
    config["routing"] = routing
    config["account_strategy"] = account_strategy
    config["retry_policy"] = retry_policy
    if transforms.get("content_mode") or transforms.get("mode"):
        config["content_mode"] = transforms.get("content_mode") or transforms.get("mode")
    if transforms.get("rewrite_prompt"):
        config["rewrite_prompt"] = transforms["rewrite_prompt"]
    if retry_policy.get("max_retries") is not None:
        failure = dict(task.failure_policy or {})
        failure["max_retries"] = retry_policy["max_retries"]
        task.failure_policy = failure
    return config


def _with_source_filter_defaults(config: dict[str, Any]) -> dict[str, Any]:
    next_config = dict(config or {})
    next_config["filter_bot_messages"] = bool(next_config.get("filter_bot_messages", True))
    next_config["filter_admin_messages"] = bool(next_config.get("filter_admin_messages", False))
    for field in ("excluded_sender_peer_ids", "excluded_sender_usernames", "excluded_sender_names"):
        value = next_config.get(field)
        next_config[field] = [str(item).strip() for item in value or [] if str(item).strip()] if isinstance(value, list) else []
    return next_config


def _norm_sender_value(value: Any) -> str:
    return str(value or "").strip().lower()


def _norm_username(value: Any) -> str:
    return _norm_sender_value(value).lstrip("@")


def relay_source_filter_reason(message, config: dict[str, Any]) -> str:
    if bool(config.get("filter_bot_messages", True)) and bool(getattr(message, "is_bot", False)):
        return "屏蔽机器人消息"
    role = _norm_sender_value(getattr(message, "sender_role", ""))
    if bool(config.get("filter_admin_messages", False)) and role in {"admin", "owner"}:
        return "不转发群主和管理员消息"
    peer_id = _norm_sender_value(getattr(message, "sender_peer_id", ""))
    if peer_id and peer_id in {_norm_sender_value(item) for item in config.get("excluded_sender_peer_ids") or []}:
        return "命中来源不转发名单：sender_peer_id"
    username = _norm_username(getattr(message, "sender_username", ""))
    if username and username in {_norm_username(item) for item in config.get("excluded_sender_usernames") or []}:
        return "命中来源不转发名单：@username"
    name = _norm_sender_value(getattr(message, "sender_name", ""))
    if name and name in {_norm_sender_value(item) for item in config.get("excluded_sender_names") or []}:
        return "昵称兜底命中来源不转发名单"
    return ""


def apply_transform_rules(content: str, transforms: dict[str, Any]) -> str:
    text = content or ""
    link_pattern = r"https?://\S+|t\.me/\S+"
    if transforms.get("remove_mentions"):
        text = re.sub(r"@\w+", "", text)
    if transforms.get("remove_links"):
        text = re.sub(link_pattern, "", text)
    replacement = transforms.get("replace_links")
    if replacement is not None:
        if isinstance(replacement, dict):
            text = re.sub(link_pattern, lambda match: str(replacement.get(match.group(0), replacement.get("*", ""))), text)
        else:
            text = re.sub(link_pattern, str(replacement), text)
    for source, target in (transforms.get("keyword_replacements") or {}).items():
        text = text.replace(str(source), str(target))
    if transforms.get("strip_source_attribution"):
        text = re.sub(r"(?m)^\s*(来源|转自|via)[:：].*$", "", text)
    prefix = str(transforms.get("prefix") or "")
    suffix = str(transforms.get("suffix") or "")
    text = f"{prefix}{text}{suffix}"
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def passes_relay_filters(content: str, sender_id: str, message_type: str, filters: dict) -> bool:
    text = content or ""
    whitelist = [str(item).lower() for item in filters.get("keyword_whitelist") or [] if str(item).strip()]
    blacklist = [str(item).lower() for item in filters.get("keyword_blacklist") or [] if str(item).strip()]
    if whitelist and not any(item in text.lower() for item in whitelist):
        return False
    if blacklist and any(item in text.lower() for item in blacklist):
        return False
    if filters.get("min_message_length") and len(text) < int(filters["min_message_length"]):
        return False
    if filters.get("max_message_length") and len(text) > int(filters["max_message_length"]):
        return False
    if sender_id and sender_id in {str(item) for item in filters.get("blocked_user_ids") or []}:
        return False
    allowed = {str(item) for item in filters.get("allowed_media_types") or []}
    if allowed and message_type not in allowed:
        return False
    is_text = message_type in {"text", "文本", ""}
    if filters.get("only_with_media") and is_text:
        return False
    if filters.get("only_text") and not is_text:
        return False
    expression = filters.get("expression")
    if expression and not _passes_filter_expression(text, sender_id, message_type, expression):
        return False
    return True


def _relay_rule_trace(config: dict[str, Any], source_group_id: int, target_group_id: int, original: str, transformed: str, account_id: int) -> dict[str, Any]:
    filters = config.get("filters") or {}
    transforms = config.get("transforms") or {}
    routing = config.get("routing") or {}
    account_strategy = config.get("account_strategy") or {}
    filter_hits = _filter_hit_summary(original, filters)
    transform_hits = _transform_hit_summary(transforms, original, transformed)
    routing_summary = _routing_hit_summary(routing, source_group_id, target_group_id, original)
    strategy_mode = str(account_strategy.get("mode") or "round_robin")
    summary_parts = [
        *(f"过滤:{item}" for item in filter_hits),
        *(f"转换:{item}" for item in transform_hits),
        f"路由:{routing_summary}",
        f"账号:{strategy_mode}#{account_id}",
    ]
    return {
        "summary": " / ".join(summary_parts),
        "filters": filter_hits,
        "transforms": transform_hits,
        "routing": routing_summary,
        "account_strategy": {"mode": strategy_mode, "account_id": account_id},
    }


def _filter_hit_summary(content: str, filters: dict[str, Any]) -> list[str]:
    text = (content or "").lower()
    hits: list[str] = []
    whitelist = [str(item) for item in filters.get("keyword_whitelist") or [] if str(item).strip()]
    blacklist = [str(item) for item in filters.get("keyword_blacklist") or [] if str(item).strip()]
    matched_whitelist = [item for item in whitelist if item.lower() in text]
    matched_blacklist = [item for item in blacklist if item.lower() in text]
    if matched_whitelist:
        hits.append("白名单 " + ",".join(matched_whitelist[:5]))
    if matched_blacklist:
        hits.append("黑名单 " + ",".join(matched_blacklist[:5]))
    if filters.get("min_message_length") is not None:
        hits.append(f"最小长度 {filters['min_message_length']}")
    if filters.get("max_message_length") is not None:
        hits.append(f"最大长度 {filters['max_message_length']}")
    if filters.get("only_text"):
        hits.append("仅文本")
    if filters.get("only_with_media"):
        hits.append("仅媒体")
    if filters.get("expression"):
        hits.append("组合条件")
    return hits or ["默认通过"]


def relay_filter_expression_reason(content: str, sender_id: str, message_type: str, filters: dict[str, Any]) -> str:
    expression = filters.get("expression")
    if not expression:
        return ""
    conditions = _expression_conditions(expression)
    if not conditions:
        return ""
    failed = [
        _expression_condition_label(condition)
        for condition in conditions
        if not _matches_expression_condition(content or "", sender_id, message_type, condition)
    ]
    mode = str(expression.get("mode") or expression.get("logic") or "all").lower() if isinstance(expression, dict) else "all"
    if mode in {"any", "or", "任一"}:
        return "组合条件未命中任一项：" + "；".join(_expression_condition_label(condition) for condition in conditions[:5])
    return "组合条件未通过：" + "；".join(failed[:5])


def _passes_filter_expression(content: str, sender_id: str, message_type: str, expression: Any) -> bool:
    conditions = _expression_conditions(expression)
    if not conditions:
        return True
    mode = str(expression.get("mode") or expression.get("logic") or "all").lower() if isinstance(expression, dict) else "all"
    results = [_matches_expression_condition(content, sender_id, message_type, condition) for condition in conditions]
    if mode in {"any", "or", "任一"}:
        return any(results)
    return all(results)


def _expression_conditions(expression: Any) -> list[dict[str, Any]]:
    if isinstance(expression, dict):
        raw = expression.get("conditions") or expression.get("rules") or []
    elif isinstance(expression, list):
        raw = expression
    else:
        raw = []
    return [item for item in raw if isinstance(item, dict)]


def _matches_expression_condition(content: str, sender_id: str, message_type: str, condition: dict[str, Any]) -> bool:
    if condition.get("conditions") or condition.get("rules"):
        return _passes_filter_expression(content, sender_id, message_type, condition)
    field = str(condition.get("field") or condition.get("type") or "content").lower()
    operator = str(condition.get("operator") or condition.get("op") or "contains").lower()
    value = condition.get("value")
    if field in {"content", "text", "message"}:
        left = content or ""
        return _match_text_condition(left, operator, value)
    if field in {"sender", "sender_id", "user", "user_id"}:
        return _match_text_condition(str(sender_id or ""), operator, value)
    if field in {"message_type", "media_type", "type"}:
        return _match_text_condition(str(message_type or "text"), operator, value)
    if field in {"length", "message_length", "content_length"}:
        return _match_number_condition(len(content or ""), operator, value)
    return True


def _match_text_condition(left: str, operator: str, value: Any) -> bool:
    left_text = str(left or "").lower()
    values = _as_str_list(value)
    if operator in {"contains", "include", "包含"}:
        return bool(values) and any(item in left_text for item in values)
    if operator in {"not_contains", "exclude", "不包含"}:
        return not any(item in left_text for item in values)
    if operator in {"eq", "equals", "=", "等于"}:
        return bool(values) and left_text in values
    if operator in {"neq", "!=", "not_equals", "不等于"}:
        return left_text not in values
    if operator in {"in", "one_of", "属于"}:
        return bool(values) and left_text in values
    if operator in {"not_in", "不属于"}:
        return left_text not in values
    return True


def _match_number_condition(left: int, operator: str, value: Any) -> bool:
    try:
        right = float(value)
    except (TypeError, ValueError):
        return True
    if operator in {"gte", ">=", "min", "至少"}:
        return left >= right
    if operator in {"lte", "<=", "max", "至多"}:
        return left <= right
    if operator in {"gt", ">", "大于"}:
        return left > right
    if operator in {"lt", "<", "小于"}:
        return left < right
    if operator in {"eq", "=", "等于"}:
        return left == right
    return True


def _expression_condition_label(condition: dict[str, Any]) -> str:
    if condition.get("conditions") or condition.get("rules"):
        nested = _expression_conditions(condition)
        mode = condition.get("mode") or condition.get("logic") or "all"
        return f"组合条件 {mode}({len(nested)})"
    field = condition.get("field") or condition.get("type") or "content"
    operator = condition.get("operator") or condition.get("op") or "contains"
    value = condition.get("value")
    if isinstance(value, list):
        value_text = ",".join(str(item) for item in value[:5])
    else:
        value_text = str(value)
    return f"{field} {operator} {value_text}".strip()


def _transform_hit_summary(transforms: dict[str, Any], original: str, transformed: str) -> list[str]:
    hits: list[str] = []
    for key, label in [
        ("remove_mentions", "移除提及"),
        ("remove_links", "移除链接"),
        ("replace_links", "替换链接"),
        ("strip_source_attribution", "移除来源"),
        ("prefix", "前缀"),
        ("suffix", "后缀"),
        ("keyword_replacements", "关键词替换"),
    ]:
        if transforms.get(key):
            hits.append(label)
    if original != transformed and not hits:
        hits.append("内容改写")
    return hits or ["未转换"]


def _routing_hit_summary(routing: dict[str, Any], source_group_id: int, target_group_id: int, content: str) -> str:
    source_map = routing.get("source_group_map") or routing.get("source_to_targets") or {}
    mapped = source_map.get(str(source_group_id)) if isinstance(source_map, dict) else None
    if mapped is None and isinstance(source_map, dict):
        mapped = source_map.get(source_group_id)
    if target_group_id in _as_int_list(mapped):
        return f"源群映射->{target_group_id}"
    text = (content or "").lower()
    for route in routing.get("routes") or []:
        if not isinstance(route, dict):
            continue
        source_ids = _as_int_list(route.get("source_group_ids") or route.get("source_groups"))
        if source_ids and source_group_id not in source_ids:
            continue
        target_ids = _as_int_list(route.get("target_group_ids") or route.get("targets"))
        if target_group_id not in target_ids:
            continue
        keywords = _as_str_list(route.get("keywords") or route.get("keyword"))
        if not keywords or any(keyword in text for keyword in keywords):
            return f"组合路由->{target_group_id}"
    for route in routing.get("keyword_routes") or []:
        if not isinstance(route, dict):
            continue
        target_ids = _as_int_list(route.get("target_group_ids") or route.get("targets"))
        keywords = _as_str_list(route.get("keywords") or route.get("keyword"))
        if target_group_id in target_ids and keywords and any(keyword in text for keyword in keywords):
            return f"关键词路由->{target_group_id}"
    return f"默认路由->{target_group_id}"


def resolve_relay_target_ids(config: dict[str, Any], source_group_id: int, content: str) -> list[int]:
    routing = config.get("routing") or {}
    default_ids = _as_int_list(routing.get("default_target_group_ids") or routing.get("target_group_ids"))
    if not default_ids:
        default_ids = _as_int_list(config.get("target_group_ids"))
    if not default_ids and config.get("target_group_id"):
        default_ids = _as_int_list(config.get("target_group_id"))

    routed_ids: list[int] = []
    source_map = routing.get("source_group_map") or routing.get("source_to_targets") or {}
    mapped = source_map.get(str(source_group_id)) if isinstance(source_map, dict) else None
    if mapped is None and isinstance(source_map, dict):
        mapped = source_map.get(source_group_id)
    routed_ids.extend(_as_int_list(mapped))

    for route in routing.get("routes") or []:
        if not isinstance(route, dict):
            continue
        source_ids = _as_int_list(route.get("source_group_ids") or route.get("source_groups"))
        if source_ids and source_group_id not in source_ids:
            continue
        keywords = _as_str_list(route.get("keywords") or route.get("keyword"))
        if keywords and not any(keyword in (content or "").lower() for keyword in keywords):
            continue
        routed_ids.extend(_as_int_list(route.get("target_group_ids") or route.get("targets")))

    for route in routing.get("keyword_routes") or []:
        if not isinstance(route, dict):
            continue
        keywords = _as_str_list(route.get("keywords") or route.get("keyword"))
        if keywords and any(keyword in (content or "").lower() for keyword in keywords):
            routed_ids.extend(_as_int_list(route.get("target_group_ids") or route.get("targets")))

    return _unique_ints(routed_ids or default_ids)


def _authorized_relay_targets(session: Session, task: Task, config: dict[str, Any], source_group_id: int, content: str) -> list[TgGroup]:
    targets, _created = _relay_targets_with_membership(session, task, config, source_group_id, content)
    return targets


def _relay_targets_with_membership(session: Session, task: Task, config: dict[str, Any], source_group_id: int, content: str) -> tuple[list[TgGroup], int]:
    targets: list[TgGroup] = []
    created = 0
    seen: set[int] = set()
    target_ids = resolve_relay_target_ids(config, source_group_id, content)
    for target_id in _unique_ints(target_ids):
        target = session.get(TgGroup, target_id)
        if (
            target
            and target.tenant_id == task.tenant_id
            and target.auth_status == GroupAuthStatus.AUTHORIZED.value
            and _relay_target_allows_outbound(session, target)
        ):
            seen.add(target.id)
            targets.append(target)
    for operation_target_id in _relay_target_operation_ids(config):
        operation_target = _operation_target_from_id(session, task.tenant_id, operation_target_id)
        if not operation_target or operation_target.target_type != "group":
            continue
        if not _relay_operation_target_allows_outbound(session, task, operation_target):
            continue
        gate = gate_channel_membership(session, task, operation_target, require_send=True)
        created += gate.created
        if not gate.ready:
            continue
        group = linked_channel_group(session, operation_target, create=False)
        if (
            group
            and group.tenant_id == task.tenant_id
            and group.id not in seen
            and _relay_target_allows_outbound(session, group)
        ):
            seen.add(group.id)
            targets.append(group)
    return targets, created


def _operation_target_from_id(session: Session, tenant_id: int, target_id: Any) -> OperationTarget | None:
    try:
        operation_target_id = int(target_id or 0)
    except (TypeError, ValueError):
        return None
    if not operation_target_id:
        return None
    target = session.get(OperationTarget, operation_target_id)
    if not target or target.tenant_id != tenant_id:
        return None
    return target


def _relay_target_allows_outbound(session: Session, group: TgGroup) -> bool:
    from app.services.outbound_target_gate import group_lifecycle_allows_outbound

    return group_lifecycle_allows_outbound(session, group) is None


def _relay_operation_target_allows_outbound(
    session: Session,
    task: Task,
    target: OperationTarget,
) -> bool:
    from app.services.outbound_target_gate import evaluate_outbound_target_gate

    return evaluate_outbound_target_gate(
        session,
        target=target,
        tenant_id=task.tenant_id,
        outbound_peer=target.tg_peer_id,
        require_identity=True,
        include_group_policy=False,
    ) is None


def _relay_target_operation_ids(config: dict[str, Any]) -> list[int]:
    routing = config.get("routing") or {}
    ids = _as_int_list(config.get("target_operation_target_ids"))
    ids.extend(_as_int_list(config.get("target_operation_target_id")))
    ids.extend(_as_int_list(routing.get("default_target_operation_target_ids") or routing.get("target_operation_target_ids")))
    for route in routing.get("routes") or []:
        if isinstance(route, dict):
            ids.extend(_as_int_list(route.get("target_operation_target_ids") or route.get("operation_target_ids")))
    return _unique_ints(ids)


def _select_relay_accounts(session: Session, task: Task, config: dict[str, Any], target_group_id: int) -> list[Any]:
    account_config = dict(task.account_config or {})
    strategy = config.get("account_strategy") or {}
    account_ids = _as_int_list(config.get("send_account_ids") or strategy.get("account_ids") or strategy.get("send_account_ids"))
    if account_ids:
        account_config["selection_mode"] = "manual"
        account_config["account_ids"] = account_ids
        account_config["max_concurrent"] = max(int(account_config.get("max_concurrent") or 0), len(account_ids))
    return select_task_accounts(session, task.tenant_id, account_config, target_group_id=target_group_id)


def _pick_relay_account(accounts: list[Any], target_id: int, source_id: int, original: str, config: dict[str, Any], offsets: dict[str, int]) -> Any:
    strategy = config.get("account_strategy") or {}
    mode = str(strategy.get("mode") or "round_robin").strip().lower()
    by_id = {int(account.id): account for account in accounts}
    mapped = _strategy_account_id(strategy.get("target_account_map") or strategy.get("target_accounts"), target_id)
    if mapped in by_id:
        return by_id[mapped]
    fixed = _first_account_id(strategy, "account_id", "fixed_account_id", "default_account_id")
    if mode in {"fixed", "固定账号"} and fixed in by_id:
        return by_id[fixed]
    if mode in {"target_sticky", "target_group_sticky", "目标群粘性"}:
        return _offset_account(accounts, f"target:{target_id}", offsets, base_seed=f"target:{target_id}")
    if mode in {"source_target_sticky", "源群目标群粘性"}:
        return _offset_account(accounts, f"source:{source_id}:target:{target_id}", offsets, base_seed=f"source:{source_id}:target:{target_id}")
    if mode in {"random", "随机"}:
        return accounts[_stable_index(f"{target_id}:{source_id}:{original}", len(accounts))]
    if mode in {"weighted_random", "weight_random", "权重随机"}:
        return _weighted_account(accounts, strategy.get("weights") or {}, f"{target_id}:{source_id}:{original}")
    return _offset_account(accounts, f"round_robin:{target_id}", offsets)


def _offset_account(accounts: list[Any], key: str, offsets: dict[str, int], *, base_seed: str | None = None) -> Any:
    base = _stable_index(base_seed, len(accounts)) if base_seed else 0
    offset = offsets.get(key, 0)
    offsets[key] = offset + 1
    return accounts[(base + offset) % len(accounts)]


def _weighted_account(accounts: list[Any], weights: dict[str, Any], seed: str) -> Any:
    weighted: list[tuple[Any, int]] = []
    for account in accounts:
        raw = weights.get(str(account.id), weights.get(account.id, 1)) if isinstance(weights, dict) else 1
        try:
            weight = max(0, int(raw))
        except (TypeError, ValueError):
            weight = 1
        if weight > 0:
            weighted.append((account, weight))
    if not weighted:
        return accounts[_stable_index(seed, len(accounts))]
    pick = _stable_index(seed, sum(weight for _, weight in weighted))
    cursor = 0
    for account, weight in weighted:
        cursor += weight
        if pick < cursor:
            return account
    return weighted[-1][0]


def _strategy_account_id(mapping: Any, target_id: int) -> int | None:
    if not isinstance(mapping, dict):
        return None
    raw = mapping.get(str(target_id), mapping.get(target_id))
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _first_account_id(strategy: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        try:
            return int(strategy[key])
        except (KeyError, TypeError, ValueError):
            continue
    return None


def _as_int_list(value: Any) -> list[int]:
    if value is None:
        return []
    if isinstance(value, int):
        return [value]
    if isinstance(value, str):
        return [int(item) for item in re.split(r"[,，\s]+", value) if item.strip().isdigit()]
    if isinstance(value, dict):
        return []
    try:
        return [int(item) for item in value if str(item).strip()]
    except (TypeError, ValueError):
        return []


def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.lower()] if value.strip() else []
    try:
        return [str(item).lower() for item in value if str(item).strip()]
    except TypeError:
        return [str(value).lower()] if str(value).strip() else []


def _unique_ints(values: list[int]) -> list[int]:
    seen: set[int] = set()
    result: list[int] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _stable_index(seed: str | None, modulo: int) -> int:
    if modulo <= 0:
        return 0
    if not seed:
        return 0
    return int(hashlib.sha1(seed.encode("utf-8")).hexdigest(), 16) % modulo


def _content_hash(content: str) -> str:
    return hashlib.sha1((content or "").encode("utf-8")).hexdigest()[:12]


def _bound_rule_version(session: Session, task: Task) -> RuleSetVersion | None:
    return bound_rule_version(session, task)


__all__ = [
    "apply_transform_rules",
    "build_plan",
    "effective_relay_config",
    "passes_relay_filters",
    "relay_filter_expression_reason",
    "relay_source_filter_reason",
    "resolve_relay_target_ids",
]

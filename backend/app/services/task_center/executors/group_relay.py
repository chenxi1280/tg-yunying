from __future__ import annotations

import hashlib
import re
from typing import Any

from sqlalchemy.orm import Session

from app.models import GroupAuthStatus, RuleSet, RuleSetVersion, Task, TgGroup
from app.services.content_filters import filter_outbound_content
from app.services.group_listeners import collect_group_context, recent_context_messages

from ..account_pool import select_task_accounts
from ..ai_generator import rewrite_relay_content
from ..fingerprints import is_duplicate, remember_fingerprint
from ..listener_runtime import should_collect_listener
from ..pacing import schedule_times
from ..payloads import SendMessagePayload, create_send_action
from .common import add_tokens, stats_inc


def build_plan(session: Session, task: Task) -> int:
    config = effective_relay_config(session, task)
    account_cache: dict[int, list[Any]] = {}
    candidate_actions: list[tuple[TgGroup, int, str, str, str]] = []
    monitor_account_ids = [int(account_id) for account_id in config.get("monitor_account_ids") or []]
    for item in [item for item in config.get("source_groups") or [] if item.get("is_active", True)]:
        source = session.get(TgGroup, int(item.get("group_id") or 0))
        if not source or source.tenant_id != task.tenant_id:
            continue
        if should_collect_listener("group", source.id, window_seconds=source.listener_interval_seconds):
            collect_group_context(session, source, monitor_account_ids or None)
        for message in reversed(recent_context_messages(session, source, source.listener_context_limit)):
            if not passes_relay_filters(message.content, message.sender_peer_id, message.message_type, config.get("filters") or {}):
                continue
            targets = _authorized_relay_targets(session, task, config, source.id, message.content)
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
                filtered = filter_outbound_content(session, tenant_id=task.tenant_id, group=target, content=rewritten, reject_mentions=True, reject_replies=True)
                if not filtered.ok:
                    stats_inc(task, "failure_count")
                    remember_fingerprint(session, task.tenant_id, source_fingerprint_key, message.content)
                    continue
                candidate_actions.append((target, source.id, message.content, filtered.content, f"{source.title} / {message.sender_name}"))
                remember_fingerprint(session, task.tenant_id, source_fingerprint_key, message.content)
    if not candidate_actions:
        return 0
    times = schedule_times(len(candidate_actions), task.pacing_config or {})
    batch_index = int((task.stats or {}).get("total_rounds") or 0) + 1
    relay_batch_id = f"{task.id}:batch:{batch_index}"
    created = 0
    target_offsets: dict[str, int] = {}
    for index, (target, source_id, original, content, source_info) in enumerate(candidate_actions):
        accounts = account_cache.get(target.id) or []
        if not accounts:
            stats_inc(task, "failure_count")
            continue
        account = _pick_relay_account(accounts, target.id, source_id, original, config, target_offsets)
        create_send_action(
            session,
            task,
            account.id,
            times[index],
            SendMessagePayload(
                chat_id=target.tg_peer_id,
                group_id=target.id,
                target_display=target.title,
                message_text=content,
                original_text=original,
                review_approved=True,
                relay_batch_id=relay_batch_id,
                relay_event_id=f"event:{source_id}:{_content_hash(original)}",
                source_group_id=source_id,
                source_info=source_info,
                rule_set_id=config.get("rule_set_id"),
                rule_set_version_id=config.get("rule_set_version_id"),
            ),
        )
        created += 1
    stats_inc(task, "total_rounds")
    return created


def effective_relay_config(session: Session, task: Task) -> dict[str, Any]:
    config = dict(task.type_config or {})
    version = _bound_rule_version(session, task)
    if not version:
        return config
    transforms = dict(version.transforms or {})
    routing = dict(version.routing or {})
    account_strategy = dict(version.account_strategy or {})
    retry_policy = dict(version.retry_policy or {})
    config["rule_set_id"] = version.rule_set_id
    config["rule_set_version_id"] = version.id
    config["filters"] = dict(version.filters or {})
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
    return True


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
    targets: list[TgGroup] = []
    for target_id in resolve_relay_target_ids(config, source_group_id, content):
        target = session.get(TgGroup, target_id)
        if target and target.tenant_id == task.tenant_id and target.auth_status == GroupAuthStatus.AUTHORIZED.value:
            targets.append(target)
    return targets


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
    config = task.type_config or {}
    version_id = int(config.get("rule_set_version_id") or 0)
    if version_id:
        version = session.get(RuleSetVersion, version_id)
        if version and version.tenant_id == task.tenant_id:
            return version
        task.last_error = "绑定的规则版本不存在"
        return None
    rule_set_id = int(config.get("rule_set_id") or 0)
    if not rule_set_id:
        return None
    rule_set = session.get(RuleSet, rule_set_id)
    if not rule_set or rule_set.tenant_id != task.tenant_id:
        task.last_error = "绑定的规则集不存在"
        return None
    if not rule_set.active_version_id:
        task.last_error = "绑定的规则集没有已发布版本"
        return None
    version = session.get(RuleSetVersion, rule_set.active_version_id)
    if not version or version.tenant_id != task.tenant_id or version.rule_set_id != rule_set.id:
        task.last_error = "绑定的活动规则版本不存在"
        return None
    return version


__all__ = ["apply_transform_rules", "build_plan", "effective_relay_config", "passes_relay_filters", "resolve_relay_target_ids"]

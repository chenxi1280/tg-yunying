from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AccountProxy, BotProtocolSample, OperationTarget, Task, TgAccount
from app.services._common import _now

from ..account_pool import select_task_accounts
from ..payloads import SearchJoinPayload, create_search_join_action
from ..stats import search_join_hourly_execution


@dataclass(frozen=True)
class SearchJoinPlan:
    bot_username: str
    keyword_hash: str
    target: OperationTarget | None
    hourly: dict


def build_plan(session: Session, task: Task) -> int:
    config = task.type_config or {}
    bot_username = _first_bot_username(config)
    if not _protocol_sample_ready(session, task.tenant_id, bot_username):
        return _block(task, "protocol_sample_missing", f"search_join protocol sample missing: {bot_username}")
    if not _keyword_hashes(config):
        return _block(task, "keyword_hash_missing", "search_join keyword hash missing")
    hourly = search_join_hourly_execution(session, task, _now())
    plan_count = _plan_count(config, hourly)
    if plan_count <= 0:
        return _record_hourly(task, hourly, 0, {})
    accounts = select_task_accounts(session, task.tenant_id, task.account_config or {}, limit=plan_count, enforce_capacity=False)
    if not accounts:
        return _block(task, "account_unavailable", "没有可用账号，等待账号恢复后继续执行")
    plan = SearchJoinPlan(bot_username=bot_username, keyword_hash="", target=_target(session, task), hourly=hourly)
    created = 0
    for account in accounts[:plan_count]:
        payload = _payload(session, config, plan, created, account)
        create_search_join_action(session, task, account.id, _now(), payload)
        created += 1
    task.last_error = ""
    return _record_hourly(task, hourly, created, {})


def _payload(session: Session, config: dict, plan: SearchJoinPlan, index: int, account: TgAccount) -> SearchJoinPayload:
    keyword_hashes = _keyword_hashes(config)
    keyword_hash = str(keyword_hashes[index % len(keyword_hashes)])
    keyword_ciphertexts = list(config.get("keyword_text_ciphertexts") or [])
    keyword_text_ciphertext = str(keyword_ciphertexts[index % len(keyword_ciphertexts)] if keyword_ciphertexts else "")
    target = plan.target
    return SearchJoinPayload(
        execution_mode="mtproto_userbot",
        bot_username=plan.bot_username,
        keyword_hash=keyword_hash,
        keyword_text_ciphertext=keyword_text_ciphertext,
        target_operation_target_id=int(config.get("target_operation_target_id") or 0) or None,
        target_group_id=int(config.get("target_group_id") or 0) or None,
        target_username=target.username if target else "",
        safe_navigation=_safe_navigation(config),
        search_visibility_attribution=_attribution(config),
        post_join_policy=str(config.get("post_join_policy") or "stay_joined"),
        hourly_execution=dict(plan.hourly),
        linked_task_policy=list(config.get("post_join_task_links") or []),
        runtime_environment=_runtime_environment(session, account),
    )


def _runtime_environment(session: Session, account: TgAccount) -> dict[str, str]:
    proxy_id = int(account.proxy_id or 0)
    proxy = session.get(AccountProxy, proxy_id) if proxy_id else None
    if not proxy or proxy.status != "healthy" or proxy.alert_status != "normal":
        return {"proxy_egress_guard": "missing"}
    return {
        "proxy_egress_guard": "verified",
        "proxy_id": str(proxy.id),
        "proxy_name": proxy.name,
    }


def _safe_navigation(config: dict) -> dict:
    pre_max = int(config.get("pre_join_decoy_click_max") or 0)
    post_max = int(config.get("post_join_safe_navigation_max") or 0)
    return {
        "pre_join_decoy_click_max": pre_max,
        "post_join_safe_navigation_max": post_max,
        "total_max": pre_max + post_max,
        "decoy_join_enabled": bool(config.get("decoy_join_enabled") or False),
        "allowed_button_effect": "navigate_only",
    }


def _attribution(config: dict) -> dict:
    return {
        "target_relevance_score": config.get("target_relevance_score"),
        "target_content_health": config.get("target_content_health") or "unknown",
        "jisou_ecosystem_status": config.get("jisou_ecosystem_status") or "unknown",
        "paid_keyword_ad_status": config.get("paid_keyword_ad_status") or "unknown",
        "rank_observation_counts_action_success": False,
    }


def _target(session: Session, task: Task) -> OperationTarget | None:
    target_id = int((task.type_config or {}).get("target_operation_target_id") or 0)
    target = session.get(OperationTarget, target_id) if target_id else None
    if target and target.tenant_id == task.tenant_id:
        return target
    return None


def _first_bot_username(config: dict) -> str:
    bots = config.get("search_bots") or []
    first = bots[0] if bots and isinstance(bots[0], dict) else {}
    return str(first.get("username") or "").strip().lstrip("@")


def _keyword_hashes(config: dict) -> list[str]:
    return [str(item).strip().lower() for item in config.get("keyword_hashes") or [] if str(item).strip()]


def _protocol_sample_ready(session: Session, tenant_id: int, bot_username: str) -> bool:
    if not bot_username:
        return False
    statement = select(BotProtocolSample.id).where(
        BotProtocolSample.tenant_id == tenant_id,
        BotProtocolSample.bot_username == bot_username,
        BotProtocolSample.sample_type == "search_results",
        BotProtocolSample.is_active.is_(True),
        BotProtocolSample.pii_scrubbed.is_(True),
    )
    return session.scalar(statement.limit(1)) is not None


def _plan_count(config: dict, hourly: dict) -> int:
    if int(config.get("hourly_min_successful_joins") or 0) <= 0:
        return 0
    per_round = int(config.get("actions_per_round") or 1)
    return max(0, min(per_round, int(hourly.get("deficit") or 0), int(hourly.get("capacity") or 0)))


def _block(task: Task, code: str, message: str) -> int:
    task.last_error = message
    _record_hourly(task, search_join_hourly_execution_stub(code), 0, {code: 1})
    return 0


def _record_hourly(task: Task, hourly: dict, planned_count: int, blockers: dict) -> int:
    stats = dict(task.stats or {})
    search_join_stats = dict(stats.get("search_join_stats") or {})
    hourly_execution = dict(hourly)
    hourly_execution["last_planned_count"] = planned_count
    hourly_execution["last_blockers"] = dict(blockers)
    search_join_stats["hourly_execution"] = hourly_execution
    stats["search_join_stats"] = search_join_stats
    task.stats = stats
    return planned_count


def search_join_hourly_execution_stub(code: str) -> dict:
    return {
        "bucket": _now().replace(minute=0, second=0, microsecond=0).isoformat(),
        "status": "blocked",
        "goal": 0,
        "success_count": 0,
        "future_open_count": 0,
        "overdue_open_count": 0,
        "deficit": 0,
        "capacity": 0,
        "max_actions_per_hour": 0,
        "block_code": code,
    }


__all__ = ["build_plan"]

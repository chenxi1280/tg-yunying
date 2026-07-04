from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Action, BotProtocolSample, OperationTarget, Task, TgAccount
from app.services.client_metadata import SearchJoinEnvironment, ensure_search_join_environment
from app.services._common import _now

from ..account_pool import select_task_accounts
from ..payloads import SearchJoinPayload, create_search_join_action
from ..search_join_pacing import PacingStats, account_base_allowed, keyword_allowed, pacing_window, planned_action_decision, should_skip_window, task_daily_capacity
from ..stats import search_join_hourly_execution


@dataclass(frozen=True)
class SearchJoinPlan:
    bot_username: str
    keyword_hash: str
    target: OperationTarget | None
    hourly: dict


@dataclass(frozen=True)
class PayloadInput:
    config: dict
    plan: SearchJoinPlan
    index: int
    account: TgAccount
    environment: SearchJoinEnvironment


@dataclass(frozen=True)
class BehaviorSkipLookup:
    task: Task
    account_id: int
    keyword_hash: str
    scheduled_at: datetime | None


ENVIRONMENT_CANDIDATE_MULTIPLIER = 3
DEFAULT_MAX_SEARCH_JOIN_PAGES = 70


def build_plan(session: Session, task: Task) -> int:
    _lock_task_for_planning(session, task)
    config = _runtime_config(task)
    bot_username = _first_bot_username(config)
    if not _protocol_sample_ready(session, task.tenant_id, bot_username):
        return _block(task, "protocol_sample_missing", f"search_join protocol sample missing: {bot_username}")
    if not _keyword_hashes(config):
        return _block(task, "keyword_hash_missing", "search_join keyword hash missing")
    now_value = _now()
    window = pacing_window(task, now_value)
    pacing_stats = PacingStats(tenant_timezone=task.timezone or "Asia/Shanghai", local_date=window.local_date.isoformat())
    if _window_skipped(session, task, config, window, pacing_stats):
        return _record_hourly(task, search_join_hourly_execution(session, task, now_value), 0, {}, pacing_stats)
    hourly = search_join_hourly_execution(session, task, _now())
    plan_count = task_daily_capacity(session, task, window, _plan_count(config, hourly), pacing_stats)
    if plan_count <= 0:
        return _record_hourly(task, hourly, 0, {}, pacing_stats)
    accounts = select_task_accounts(session, task.tenant_id, task.account_config or {}, limit=plan_count * ENVIRONMENT_CANDIDATE_MULTIPLIER, enforce_capacity=False)
    if not accounts:
        return _block(task, "account_unavailable", "没有可用账号，等待账号恢复后继续执行")
    plan = SearchJoinPlan(bot_username=bot_username, keyword_hash="", target=_target(session, task), hourly=hourly)
    created = 0
    blockers: dict[str, int] = {}
    keyword_hashes = _keyword_hashes(config)
    for account in accounts:
        if not account_base_allowed(session, task, account.id, window, pacing_stats):
            continue
        keyword_hash = _candidate_keyword_hash(session, task, account.id, keyword_hashes, created, window, pacing_stats)
        if not keyword_hash:
            continue
        environment = _environment(session, account, blockers)
        if environment is None:
            continue
        payload = _payload(PayloadInput(config=config, plan=plan, index=created, account=account, environment=environment))
        payload = payload.model_copy(update={"keyword_hash": keyword_hash})
        _create_planned_action(session, task, account, payload, keyword_hash, window, config)
        created += 1
        if created >= plan_count:
            break
    if created <= 0:
        if pacing_stats.blocked_accounts:
            return _record_hourly(task, hourly, 0, blockers, pacing_stats)
        return _block(task, "needs_client_metadata", "搜索入群缺少可执行授权环境栈或客户端 metadata")
    task.last_error = ""
    return _record_hourly(task, hourly, created, blockers, pacing_stats)


def _runtime_config(task: Task) -> dict:
    return {**(task.type_config or {}), **(task.pacing_config or {})}


def _lock_task_for_planning(session: Session, task: Task) -> None:
    session.execute(select(Task.id).where(Task.id == task.id).with_for_update()).scalar_one_or_none()


def _window_skipped(session: Session, task: Task, config: dict, window, pacing_stats: PacingStats) -> bool:
    if should_skip_window(session, task, "daily", float(config.get("daily_skip_probability") or 0), window):
        pacing_stats.daily_skipped_by_pacing = 1
        pacing_stats.last_limit_reason = "daily_skipped_by_pacing"
        return True
    if should_skip_window(session, task, "hourly", float(config.get("hourly_skip_probability") or 0), window):
        pacing_stats.hourly_skipped_by_pacing = 1
        pacing_stats.last_limit_reason = "hourly_skipped_by_pacing"
        return True
    return False


def _create_planned_action(session: Session, task: Task, account: TgAccount, payload: SearchJoinPayload, keyword_hash: str, window, config: dict) -> None:
    candidate_key = f"{window.local_date.isoformat()}:{account.id}:{keyword_hash}:{payload.hourly_execution.get('bucket', '')}"
    decision = planned_action_decision(
        session,
        task,
        candidate_key,
        float(config.get("skip_probability_per_action") or 0),
        int(config.get("hourly_jitter_percent") or 0),
        int(config.get("daily_jitter_percent") or 0),
        window,
        account_id=account.id,
        keyword_hash=keyword_hash,
        base_scheduled_at=_now(),
    )
    if not decision.decision_value.get("skipped"):
        create_search_join_action(session, task, account.id, decision.scheduled_at or _now(), payload)
        return
    lookup = BehaviorSkipLookup(task, account.id, keyword_hash, decision.scheduled_at)
    if _existing_behavior_skip_action(session, lookup):
        return
    action = create_search_join_action(session, task, account.id, decision.scheduled_at or _now(), payload)
    action.status = "skipped"
    action.executed_at = _now()
    action.result = {"success": False, "skip_reason": "skipped_by_behavior_pacing"}


def _existing_behavior_skip_action(session: Session, lookup: BehaviorSkipLookup) -> Action | None:
    if lookup.scheduled_at is None:
        return None
    actions = session.scalars(
        select(Action).where(
            Action.task_id == lookup.task.id,
            Action.action_type == "search_join",
            Action.account_id == lookup.account_id,
            Action.status == "skipped",
            Action.scheduled_at == lookup.scheduled_at,
        )
    )
    return next((action for action in actions if _same_behavior_skip(action, lookup.keyword_hash)), None)


def _same_behavior_skip(action: Action, keyword_hash: str) -> bool:
    payload = action.payload or {}
    result = action.result or {}
    return payload.get("keyword_hash") == keyword_hash and result.get("skip_reason") == "skipped_by_behavior_pacing"


def _candidate_keyword_hash(session: Session, task: Task, account_id: int, keyword_hashes: list[str], offset: int, window, pacing_stats: PacingStats) -> str:
    for index in range(len(keyword_hashes)):
        keyword_hash = keyword_hashes[(offset + index) % len(keyword_hashes)]
        if keyword_allowed(session, task, account_id, keyword_hash, window, pacing_stats):
            return keyword_hash
    return ""


def _payload(payload_input: PayloadInput) -> SearchJoinPayload:
    config = payload_input.config
    keyword_hashes = _keyword_hashes(config)
    keyword_hash = str(keyword_hashes[payload_input.index % len(keyword_hashes)])
    keyword_text_ciphertext = _keyword_ciphertext(config, payload_input.index)
    target = payload_input.plan.target
    return SearchJoinPayload(
        execution_mode="mtproto_userbot",
        bot_username=payload_input.plan.bot_username,
        max_pages=_max_pages(config),
        keyword_hash=keyword_hash,
        keyword_text_ciphertext=keyword_text_ciphertext,
        authorization_id=payload_input.environment.authorization_id,
        session_role=payload_input.environment.session_role,
        client_metadata=payload_input.environment.client_metadata,
        target_operation_target_id=int(config.get("target_operation_target_id") or 0) or None,
        target_group_id=int(config.get("target_group_id") or 0) or None,
        target_username=target.username if target else "",
        target_title=target.title if target else "",
        target_peer_id=target.tg_peer_id if target else "",
        safe_navigation=_safe_navigation(config),
        search_visibility_attribution=_attribution(config),
        post_join_policy=str(config.get("post_join_policy") or "stay_joined"),
        hourly_execution=dict(payload_input.plan.hourly),
        linked_task_policy=list(config.get("post_join_task_links") or []),
        runtime_environment=_runtime_environment(payload_input.environment),
    )


def _environment(session: Session, account: TgAccount, blockers: dict[str, int]) -> SearchJoinEnvironment | None:
    try:
        environment = ensure_search_join_environment(session, account)
    except ValueError as exc:
        _count_blocker(blockers, str(exc))
        return None
    if environment is None:
        _count_blocker(blockers, "needs_client_metadata")
    return environment


def _keyword_ciphertext(config: dict, index: int) -> str:
    keyword_ciphertexts = list(config.get("keyword_text_ciphertexts") or [])
    if not keyword_ciphertexts:
        return ""
    return str(keyword_ciphertexts[index % len(keyword_ciphertexts)])


def _max_pages(config: dict) -> int:
    return int(config.get("max_pages") or DEFAULT_MAX_SEARCH_JOIN_PAGES)


def _runtime_environment(environment: SearchJoinEnvironment) -> dict[str, str]:
    return {
        "proxy_egress_guard": "verified",
        "client_metadata_guard": "verified",
        "proxy_id": str(environment.proxy_id),
        "proxy_name": environment.proxy_name,
        "proxy_binding_id": str(environment.proxy_binding_id),
        "environment_binding_id": environment.binding_id,
        "client_identity_key": environment.client_metadata["client_identity_key"],
    }


def _count_blocker(blockers: dict[str, int], code: str) -> None:
    blockers[code] = int(blockers.get(code, 0)) + 1


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
    _record_hourly(task, search_join_hourly_execution_stub(code), 0, {code: 1}, None)
    return 0


def _record_hourly(task: Task, hourly: dict, planned_count: int, blockers: dict, pacing_stats: PacingStats | None) -> int:
    stats = dict(task.stats or {})
    search_join_stats = dict(stats.get("search_join_stats") or {})
    hourly_execution = dict(hourly)
    hourly_execution["last_planned_count"] = planned_count
    hourly_execution["last_blockers"] = dict(blockers)
    search_join_stats["hourly_execution"] = hourly_execution
    if pacing_stats is not None:
        search_join_stats["pacing_limits"] = pacing_stats.as_dict()
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

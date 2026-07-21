from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.admin_chats import send_admin_chat_broadcast
from app.models import Action, BotProtocolSample, OperationTarget, Task, Tenant, TgAccount
from app.search_keywords import repair_legacy_keyword_materials
from app.security import decrypt_secret
from app.services.client_metadata import SearchJoinEnvironment, ensure_search_join_environment
from app.services._common import _now, audit
from app.services.notifications import NotificationResult, send_telegram_bot_message
from app.services.proxy_airport_subscription import (
    list_proxy_airport_subscriptions,
    select_proxy_airport_subscription_for_failover,
)
from app.timezone import as_beijing

from ..account_pool import select_task_accounts
from ..pacing import quiet_hours_active
from ..payloads import SearchJoinPayload, create_search_join_action
from ..search_click_target_progress import reconcile_search_click_target_progress
from ..search_join_config import runtime_search_join_config
from ..search_join_pacing import PacingStats, account_base_allowed, hourly_action_allowed, keyword_allowed, pacing_window, planned_action_decision, should_skip_window, task_daily_capacity
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
    keyword_hash: str
    account: TgAccount
    environment: SearchJoinEnvironment


@dataclass(frozen=True)
class BehaviorSkipLookup:
    task: Task
    account_id: int
    keyword_hash: str
    scheduled_at: datetime | None


def build_plan(session: Session, task: Task) -> int:
    _lock_task_for_planning(session, task)
    now_value = _now()
    target_progress = reconcile_search_click_target_progress(session, task, now_value=now_value)
    if target_progress.completed or target_progress.remaining_slot_count == 0:
        return 0
    config = _runtime_config(task)
    bot_username = _first_bot_username(config)
    if not _protocol_sample_ready(session, task.tenant_id, bot_username):
        return _block(task, "protocol_sample_missing", f"search_join protocol sample missing: {bot_username}")
    try:
        keyword_materials = _keyword_materials(config)
    except ValueError as exc:
        return _block(task, "keyword_material_invalid", f"search_join keyword material invalid: {exc}")
    if not keyword_materials:
        return _block(task, "keyword_material_missing", "search_join keyword hash/ciphertext material missing or mismatched")
    config = _canonical_keyword_materials(task, config, keyword_materials)
    window = pacing_window(task, now_value)
    pacing_stats = PacingStats(tenant_timezone=task.timezone or "Asia/Shanghai", local_date=window.local_date.isoformat())
    if quiet_hours_active(now_value, config, timezone_name=task.timezone):
        pacing_stats.last_limit_reason = "quiet_hours_active"
        task.last_error = ""
        return _record_hourly(
            task,
            search_join_hourly_execution(session, task, now_value, target_progress=target_progress),
            0,
            {"quiet_hours_active": 1},
            pacing_stats,
        )
    if _window_skipped(session, task, config, window, pacing_stats):
        return _record_hourly(task, search_join_hourly_execution(session, task, now_value, target_progress=target_progress), 0, {}, pacing_stats)
    hourly = search_join_hourly_execution(
        session,
        task,
        now_value,
        target_progress=target_progress,
    )
    plan_count = task_daily_capacity(session, task, window, _plan_count(config, hourly), pacing_stats)
    if target_progress.remaining_slot_count is not None:
        plan_count = min(plan_count, target_progress.remaining_slot_count)
    if plan_count <= 0:
        return _record_hourly(task, hourly, 0, {}, pacing_stats)
    if _clash_subscription_pool_unavailable(session, task.tenant_id):
        return _record_all_subscriptions_unavailable(session, task, hourly, pacing_stats)
    accounts = select_task_accounts(
        session,
        task.tenant_id,
        task.account_config or {},
        enforce_capacity=False,
        scan_all_candidates=True,
    )
    if not accounts:
        return _block(task, "account_unavailable", "没有可用账号，等待账号恢复后继续执行")
    target = _target(session, task)
    if target is None or not target.username.strip():
        return _block(task, "target_identity_missing", "搜索入群目标缺少可验证 username")
    plan = SearchJoinPlan(bot_username=bot_username, keyword_hash="", target=target, hourly=hourly)
    created = 0
    blockers: dict[str, int] = {}
    keyword_hashes = [item[0] for item in keyword_materials]
    for account in accounts:
        if not account_base_allowed(session, task, account.id, window, pacing_stats):
            continue
        keyword_hash = _candidate_keyword_hash(session, task, account.id, keyword_hashes, created, window, pacing_stats)
        if not keyword_hash:
            continue
        environment = _environment(session, account, blockers)
        if environment is None:
            continue
        payload = _payload(PayloadInput(
            config=config,
            plan=plan,
            keyword_hash=keyword_hash,
            account=account,
            environment=environment,
        ))
        action_created, blocker = _create_planned_action(session, task, account, payload, keyword_hash, window, config)
        if blocker:
            _count_blocker(blockers, blocker)
        if action_created:
            created += 1
        if created >= plan_count:
            break
    if created <= 0:
        if _should_preserve_search_join_blockers(blockers) or pacing_stats.blocked_accounts:
            return _record_hourly(task, hourly, 0, blockers, pacing_stats)
        return _block(task, "needs_client_metadata", "搜索入群缺少可执行授权环境栈或客户端 metadata")
    task.last_error = ""
    planned = _record_hourly(task, hourly, created, blockers, pacing_stats)
    reconcile_search_click_target_progress(session, task)
    return planned


def _runtime_config(task: Task) -> dict:
    return runtime_search_join_config(task)


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


def _create_planned_action(
    session: Session,
    task: Task,
    account: TgAccount,
    payload: SearchJoinPayload,
    keyword_hash: str,
    window,
    config: dict,
) -> tuple[bool, str]:
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
    scheduled_at = _scheduled_before_task_deadline(task, decision.scheduled_at or _now(), _now())
    if scheduled_at is None:
        return False, "scheduled_end_reached"
    if quiet_hours_active(scheduled_at, config, timezone_name=task.timezone):
        return False, "quiet_hours_active"
    if not hourly_action_allowed(session, task, scheduled_at, max_actions_per_hour=int(config.get("max_actions_per_hour") or 0)):
        return False, "task_hourly_limit_reached"
    decision.scheduled_at = scheduled_at
    if not decision.decision_value.get("skipped"):
        create_search_join_action(session, task, account.id, scheduled_at, payload)
        return True, ""
    lookup = BehaviorSkipLookup(task, account.id, keyword_hash, decision.scheduled_at)
    if _existing_behavior_skip_action(session, lookup):
        return True, ""
    action = create_search_join_action(session, task, account.id, scheduled_at, payload)
    action.status = "skipped"
    action.executed_at = _now()
    action.result = {"success": False, "skip_reason": "skipped_by_behavior_pacing"}
    return True, ""


def _scheduled_before_task_deadline(task: Task, scheduled_at: datetime, now_value: datetime) -> datetime | None:
    candidate = as_beijing(scheduled_at) or scheduled_at
    if task.scheduled_end is None:
        return candidate
    deadline = as_beijing(task.scheduled_end)
    current = as_beijing(now_value) or now_value
    if deadline is None or deadline <= current:
        return None
    return min(candidate, deadline - timedelta(seconds=1))


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
    keyword_hash = payload_input.keyword_hash
    keyword_text_ciphertext = _keyword_ciphertext(config, keyword_hash)
    target = payload_input.plan.target
    return SearchJoinPayload(
        execution_mode="mtproto_userbot",
        bot_username=payload_input.plan.bot_username,
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
    if _clash_subscription_pool_unavailable(session, account.tenant_id):
        _count_blocker(blockers, "airport_all_subscriptions_unavailable")
        return None
    try:
        environment = ensure_search_join_environment(session, account)
    except ValueError as exc:
        _count_blocker(blockers, str(exc))
        return None
    if environment is None:
        _count_blocker(blockers, "needs_client_metadata")
    return environment


def _clash_subscription_pool_unavailable(session: Session, tenant_id: int) -> bool:
    rows = list_proxy_airport_subscriptions(session, tenant_id=tenant_id)
    enabled = [row for row in rows if row.enabled and row.subscription_url_configured]
    return bool(enabled) and select_proxy_airport_subscription_for_failover(session, tenant_id=tenant_id) is None


def _should_preserve_search_join_blockers(blockers: dict[str, int]) -> bool:
    return bool(blockers) and set(blockers) != {"needs_client_metadata"}


def _record_all_subscriptions_unavailable(
    session: Session,
    task: Task,
    hourly: dict,
    pacing_stats: PacingStats | None,
) -> int:
    notification = _notify_all_subscriptions_unavailable(session, task)
    hourly_with_notice = {
        **hourly,
        "admin_notification_status": "sent" if notification.ok else "admin_notification_failed",
        "admin_notification_detail": notification.detail,
    }
    return _record_hourly(
        task,
        hourly_with_notice,
        0,
        {"airport_all_subscriptions_unavailable": 1},
        pacing_stats,
    )


def _notify_all_subscriptions_unavailable(session: Session, task: Task) -> NotificationResult:
    tenant = session.get(Tenant, task.tenant_id)
    if not tenant or not tenant.admin_chat_id or not tenant.telegram_bot_token_ciphertext:
        result = NotificationResult(False, "Telegram Bot token or admin chat id not configured")
        _audit_subscription_notification(session, task, result)
        return result
    bot_token = decrypt_secret(tenant.telegram_bot_token_ciphertext)
    if not bot_token:
        result = NotificationResult(False, "Telegram Bot token decrypts to empty")
        _audit_subscription_notification(session, task, result)
        return result
    summary = send_admin_chat_broadcast(
        bot_token=bot_token,
        raw_admin_chat_id=tenant.admin_chat_id,
        text=f"Clash 订阅源池全部不可用\n任务: {task.name}\n任务ID: {task.id}\n处理: 已停止生成搜索目标群点击真实操作",
        sender=send_telegram_bot_message,
    )
    result = NotificationResult(summary.ok, summary.detail)
    _audit_subscription_notification(session, task, result)
    return result


def _audit_subscription_notification(session: Session, task: Task, result: NotificationResult) -> None:
    audit(
        session,
        tenant_id=task.tenant_id,
        actor="search-join-planner",
        action="Clash订阅全部不可用通知" if result.ok else "Clash订阅全部不可用通知失败",
        target_type="task",
        target_id=str(task.id),
        detail=result.detail,
    )


def _keyword_ciphertext(config: dict, keyword_hash: str) -> str:
    for item_hash, ciphertext in _keyword_materials(config):
        if item_hash == keyword_hash:
            return ciphertext
    raise ValueError("search_join keyword ciphertext missing for keyword hash")


def _runtime_environment(environment: SearchJoinEnvironment) -> dict[str, str]:
    return {
        "proxy_egress_guard": "verified",
        "client_metadata_guard": "verified",
        "developer_app_id": str(environment.developer_app_id),
        "developer_app_api_id": str(environment.developer_app_api_id),
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
    return {
        "pre_join_decoy_click_max": pre_max,
        "post_join_safe_navigation_max": 0,
        "total_max": pre_max,
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
    return [item[0] for item in _keyword_materials(config)]


def _keyword_materials(config: dict) -> list[tuple[str, str]]:
    hashes = [str(item).strip().lower() for item in config.get("keyword_hashes") or [] if str(item).strip()]
    ciphertexts = [str(item).strip() for item in config.get("keyword_text_ciphertexts") or [] if str(item).strip()]
    if not hashes:
        return []
    if len(hashes) == len(ciphertexts):
        return [] if len(set(hashes)) != len(hashes) else list(zip(hashes, ciphertexts, strict=True))
    return repair_legacy_keyword_materials(hashes, ciphertexts)


def _canonical_keyword_materials(task: Task, config: dict, materials: list[tuple[str, str]]) -> dict:
    hashes = [item[0] for item in materials]
    ciphertexts = [item[1] for item in materials]
    if config.get("keyword_hashes") == hashes and config.get("keyword_text_ciphertexts") == ciphertexts:
        return config
    normalized = {**config, "keyword_hashes": hashes, "keyword_text_ciphertexts": ciphertexts}
    task.type_config = normalized
    return normalized


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

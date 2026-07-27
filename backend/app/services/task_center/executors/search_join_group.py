from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.admin_chats import send_admin_chat_broadcast
from app.models import Action, BotProtocolSample, OperationTarget, Task, Tenant, TgAccount, TgAccountAuthorization
from app.search_join_protocol import approved_protocol_profile, is_jisou_bot
from app.search_keywords import repair_legacy_keyword_materials
from app.security import decrypt_secret
from app.services.account_capacity import (
    ACTION_OCCUPIED_STATUSES,
    MESSAGE_TASK_OCCUPIED_STATUSES,
    AccountCapacityCache,
    AccountCapacityReservation,
    account_capacity_decision,
)
from app.services.account_capacity_bulk import capacity_setting, prime_capacity_cache
from app.services.client_metadata import SearchJoinEnvironment, ensure_search_join_environment
from app.services._common import _now, audit
from app.services.notifications import NotificationResult, send_telegram_bot_message
from app.services.proxy_airport_subscription import (
    list_proxy_airport_subscriptions,
    select_proxy_airport_subscription_for_failover,
)
from app.timezone import BEIJING_TZ, as_beijing

from ..account_pool import select_task_accounts
from ..jisou_selector_accounts import select_jisou_selector_candidates
from ..pacing import quiet_hours_active
from ..payloads import SearchJoinPayload, create_search_join_action
from ..search_click_target_progress import reconcile_search_click_target_progress
from ..search_join_config import runtime_search_join_config
from ..search_join_daily_capacity import (
    SearchJoinDailyCapacity,
    configured_account_source_capacity,
    strict_capacity_action_key,
    strict_daily_capacity,
)
from ..search_join_pacing import (
    PacingStats,
    account_base_allowed,
    hourly_action_allowed,
    hourly_source_occupancy,
    keyword_allowed,
    pacing_window,
    planned_action_decision,
    should_skip_window,
    task_daily_capacity,
)
from ..stats import search_join_hourly_execution


@dataclass(frozen=True)
class SearchJoinPlan:
    bot_username: str
    keyword_hash: str
    target: OperationTarget | None
    hourly: dict
    protocol_sample_version: str
    approved_protocol_profile: dict


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


@dataclass
class CapacityPlan:
    tenant_id: int
    scheduled_end: datetime | None
    account_ids: list[int]
    cache: AccountCapacityCache
    reservations: list[AccountCapacityReservation]


def build_plan(session: Session, task: Task) -> int:
    _lock_task_for_planning(session, task)
    now_value = _now()
    target_progress = reconcile_search_click_target_progress(session, task, now_value=now_value)
    if target_progress.completed or target_progress.remaining_slot_count == 0:
        return 0
    config = _runtime_config(task)
    bot_username = _first_bot_username(config)
    try:
        keyword_materials = _keyword_materials(config)
    except ValueError as exc:
        return _block(task, "keyword_material_invalid", f"search_join keyword material invalid: {exc}")
    if not keyword_materials:
        return _block(task, "keyword_material_missing", "search_join keyword hash/ciphertext material missing or mismatched")
    config = _canonical_keyword_materials(task, config, keyword_materials)
    window = pacing_window(task, now_value)
    pacing_stats = PacingStats(tenant_timezone=task.timezone or "Asia/Shanghai", local_date=window.local_date.isoformat())
    strict_window_skipped = bool((task.type_config or {}).get("strict_daily_target")) and _window_skipped(
        session,
        task,
        config,
        window,
        pacing_stats,
    )
    accounts = select_task_accounts(
        session,
        task.tenant_id,
        task.account_config or {},
        enforce_capacity=False,
        scan_all_candidates=True,
    )
    configured_account_count = len(accounts)
    selector_candidates = select_jisou_selector_candidates(
        session,
        task,
        accounts,
        bot_username=bot_username,
        now_value=now_value,
    )
    effective_accounts = list(selector_candidates.accounts)
    _record_planner_account_selection_warning(task, configured_account_count, len(effective_accounts))
    strict_capacity = _remaining_strict_daily_capacity(
        session,
        task,
        config,
        effective_accounts,
        now_value,
        pacing_stats,
    )
    _record_strict_capacity_snapshot(task, target_progress, strict_capacity)
    if _strict_daily_target_is_impossible(task, target_progress, strict_capacity):
        return _record_strict_capacity_blocked(task, target_progress, strict_capacity, pacing_stats)
    protocol_sample = _protocol_sample(session, task.tenant_id, bot_username)
    if protocol_sample is None:
        return _block(task, "protocol_sample_missing", f"search_join protocol sample missing: {bot_username}")
    protocol_profile = _approved_protocol_profile(protocol_sample, bot_username)
    if protocol_profile is None:
        return _block(task, "protocol_sample_invalid", f"search_join protocol sample lacks approved fingerprints: {bot_username}")
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
    if strict_window_skipped or _window_skipped(session, task, config, window, pacing_stats):
        return _record_hourly(task, search_join_hourly_execution(session, task, now_value, target_progress=target_progress), 0, {}, pacing_stats)
    hourly = search_join_hourly_execution(
        session,
        task,
        now_value,
        target_progress=target_progress,
    )
    requested_plan_count = _plan_count(
        config,
        hourly,
        target_progress=target_progress,
        strict_capacity=strict_capacity,
    )
    plan_count = task_daily_capacity(session, task, window, requested_plan_count, pacing_stats)
    if target_progress.remaining_slot_count is not None:
        plan_count = min(plan_count, target_progress.remaining_slot_count)
    if plan_count <= 0:
        return _record_hourly(task, hourly, 0, {}, pacing_stats)
    if _clash_subscription_pool_unavailable(session, task.tenant_id):
        return _record_all_subscriptions_unavailable(session, task, hourly, pacing_stats)
    if not accounts:
        return _block(task, "account_unavailable", "没有可用账号，等待账号恢复后继续执行")
    target = _target(session, task)
    if target is None or not target.username.strip():
        return _block(task, "target_identity_missing", "搜索入群目标缺少可验证 username")
    plan = SearchJoinPlan(
        bot_username=bot_username,
        keyword_hash="",
        target=target,
        hourly=hourly,
        protocol_sample_version=protocol_sample.schema_version,
        approved_protocol_profile=protocol_profile,
    )
    created = 0
    blockers: dict[str, int] = {}
    if selector_candidates.excluded_count:
        blockers["jisou_group_selector_account_excluded"] = selector_candidates.excluded_count
    accounts = effective_accounts
    if not accounts:
        task.last_error = "极搜群聊 selector 在候选账号上均不可用"
        return _record_hourly(
            task,
            hourly,
            0,
            {"jisou_group_selector_account_unavailable": selector_candidates.excluded_count},
            pacing_stats,
        )
    keyword_hashes = [item[0] for item in keyword_materials]
    planning_accounts = _planning_accounts(
        accounts,
        plan_count,
        allow_repeat=bool((task.type_config or {}).get("allow_same_account_repeat_application")),
    )
    capacity_plan = _capacity_plan(task, planning_accounts)
    candidate_offset = (
        strict_capacity.current_hour_source_occupied
        if strict_capacity is not None
        else pacing_stats.task_daily_action_count
    )
    for candidate_index, account in enumerate(planning_accounts, start=candidate_offset):
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
        action_created, blocker, reserved_at = _create_planned_action(
            session,
            task,
            account,
            payload,
            keyword_hash,
            window,
            config,
            capacity_plan,
            candidate_index=candidate_index,
            strict_capacity=strict_capacity,
        )
        if blocker:
            _count_blocker(blockers, blocker)
        if action_created:
            created += 1
        if reserved_at:
            capacity_plan.reservations.append(AccountCapacityReservation(account_id=account.id, scheduled_at=reserved_at))
        if created >= plan_count:
            break
    if created <= 0:
        if _should_preserve_search_join_blockers(blockers) or pacing_stats.blocked_accounts:
            return _record_hourly(task, hourly, 0, blockers, pacing_stats)
        return _block(task, "needs_client_metadata", "搜索入群缺少可执行授权环境栈或客户端 metadata")
    task.last_error = ""
    planned = _record_hourly(task, hourly, created, blockers, pacing_stats)
    current_progress = reconcile_search_click_target_progress(session, task, now_value=now_value)
    if strict_capacity is not None:
        refreshed_stats = PacingStats()
        refreshed_capacity = _remaining_strict_daily_capacity(
            session,
            task,
            config,
            accounts,
            now_value,
            refreshed_stats,
        )
        _record_strict_capacity_snapshot(task, current_progress, refreshed_capacity)
    return planned


def _runtime_config(task: Task) -> dict:
    return runtime_search_join_config(task)


def _planning_accounts(accounts: list[TgAccount], plan_count: int, *, allow_repeat: bool) -> list[TgAccount]:
    if not allow_repeat or len(accounts) >= plan_count:
        return accounts
    return [accounts[index % len(accounts)] for index in range(plan_count)]


def _capacity_plan(task: Task, accounts: list[TgAccount]) -> CapacityPlan:
    return CapacityPlan(
        tenant_id=task.tenant_id,
        scheduled_end=task.scheduled_end,
        account_ids=list(dict.fromkeys(account.id for account in accounts)),
        cache=AccountCapacityCache(),
        reservations=[],
    )


def _prime_capacity_plan(session: Session, capacity_plan: CapacityPlan, scheduled_at: datetime) -> None:
    cache = capacity_plan.cache
    prime_capacity_cache(
        session,
        tenant_id=capacity_plan.tenant_id,
        account_ids=capacity_plan.account_ids,
        scheduled_at=scheduled_at,
        setting=capacity_setting(session, capacity_plan.tenant_id, cache),
        exclude_action_ids=set(),
        exclude_message_task_id=None,
        action_statuses=ACTION_OCCUPIED_STATUSES,
        message_statuses=MESSAGE_TASK_OCCUPIED_STATUSES,
        cache=cache,
    )


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
    capacity_plan: CapacityPlan,
    *,
    candidate_index: int,
    strict_capacity: SearchJoinDailyCapacity | None,
) -> tuple[bool, str, datetime | None]:
    candidate_key = _candidate_key(
        task,
        account,
        keyword_hash,
        payload,
        window,
        candidate_index,
        strict_capacity,
    )
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
        return False, "scheduled_end_reached", None
    if not decision.decision_value.get("skipped"):
        scheduled_at = _next_account_capacity_slot(session, account.id, scheduled_at, capacity_plan)
        if scheduled_at is None:
            return False, "scheduled_end_reached", None
    if quiet_hours_active(scheduled_at, config, timezone_name=task.timezone):
        return False, "quiet_hours_active", None
    if not hourly_action_allowed(session, task, scheduled_at, max_actions_per_hour=int(config.get("max_actions_per_hour") or 0)):
        return False, "task_hourly_limit_reached", None
    decision.scheduled_at = scheduled_at
    action_payload = payload.model_copy(update={"planning_slot_key": candidate_key})
    if not decision.decision_value.get("skipped"):
        create_search_join_action(session, task, account.id, scheduled_at, action_payload)
        return True, "", scheduled_at
    lookup = BehaviorSkipLookup(task, account.id, keyword_hash, decision.scheduled_at)
    if _existing_behavior_skip_action(session, lookup):
        return True, "", None
    action = create_search_join_action(session, task, account.id, scheduled_at, action_payload)
    action.status = "skipped"
    action.executed_at = _now()
    action.result = {"success": False, "skip_reason": "skipped_by_behavior_pacing"}
    return True, "", None


def _candidate_key(
    task: Task,
    account: TgAccount,
    keyword_hash: str,
    payload: SearchJoinPayload,
    window,
    candidate_index: int,
    strict_capacity: SearchJoinDailyCapacity | None,
) -> str:
    if strict_capacity is not None:
        return strict_capacity_action_key(
            task.timezone,
            window.local_date,
            window.hour_start.hour,
            candidate_index,
        )
    return f"{window.local_date.isoformat()}:{account.id}:{keyword_hash}:{payload.hourly_execution.get('bucket', '')}:{candidate_index}"


def _next_account_capacity_slot(
    session: Session,
    account_id: int,
    scheduled_at: datetime,
    capacity_plan: CapacityPlan,
) -> datetime | None:
    cursor = scheduled_at
    while True:
        _prime_capacity_plan(session, capacity_plan, cursor)
        decision = account_capacity_decision(
            session,
            tenant_id=capacity_plan.tenant_id,
            account_id=account_id,
            scheduled_at=cursor,
            reservations=capacity_plan.reservations,
            cache=capacity_plan.cache,
        )
        if decision.available:
            return cursor
        deferred_at = decision.defer_until
        if deferred_at is None or deferred_at <= cursor:
            raise RuntimeError(f"search_join account capacity did not advance account_id={account_id}")
        if not _before_task_deadline(capacity_plan.scheduled_end, deferred_at):
            return None
        cursor = deferred_at


def _before_task_deadline(scheduled_end: datetime | None, scheduled_at: datetime) -> bool:
    deadline = as_beijing(scheduled_end)
    return deadline is None or scheduled_at < deadline


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
        protocol_sample_version=payload_input.plan.protocol_sample_version,
        approved_protocol_profile=payload_input.plan.approved_protocol_profile,
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
    elif not _environment_authorization_matches_account(session, account, environment):
        _count_blocker(blockers, "search_join_environment_authorization_scope_mismatch")
        return None
    return environment


def _environment_authorization_matches_account(
    session: Session,
    account: TgAccount,
    environment: SearchJoinEnvironment,
) -> bool:
    authorization = session.get(TgAccountAuthorization, environment.authorization_id)
    return bool(
        authorization
        and authorization.tenant_id == account.tenant_id
        and authorization.account_id == account.id
        and authorization.role == environment.session_role
    )


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


def _protocol_sample(session: Session, tenant_id: int, bot_username: str) -> BotProtocolSample | None:
    if not bot_username:
        return None
    statement = select(BotProtocolSample).where(
        BotProtocolSample.tenant_id == tenant_id,
        BotProtocolSample.bot_username == bot_username,
        BotProtocolSample.sample_type == "search_results",
        BotProtocolSample.is_active.is_(True),
        BotProtocolSample.pii_scrubbed.is_(True),
    )
    return session.scalar(statement.order_by(BotProtocolSample.captured_at.desc(), BotProtocolSample.id.desc()).limit(1))


def _approved_protocol_profile(sample: BotProtocolSample, bot_username: str) -> dict | None:
    if not is_jisou_bot(bot_username):
        return {}
    return approved_protocol_profile(sample.structure_json)


def _plan_count(
    config: dict,
    hourly: dict,
    *,
    target_progress=None,
    strict_capacity: SearchJoinDailyCapacity | None = None,
) -> int:
    if strict_capacity is not None:
        return _strict_plan_count(target_progress, strict_capacity)
    if int(config.get("hourly_min_successful_joins") or 0) <= 0:
        return 0
    per_round = int(config.get("actions_per_round") or 1)
    return max(0, min(per_round, int(hourly.get("deficit") or 0), int(hourly.get("capacity") or 0)))


def _strict_plan_count(target_progress, capacity: SearchJoinDailyCapacity) -> int:
    remaining = max(0, int(getattr(target_progress, "remaining_slot_count", 0) or 0))
    if remaining <= 0 or capacity.remaining_executable_hours <= 0:
        return 0
    required_this_window = (remaining + capacity.remaining_executable_hours - 1) // capacity.remaining_executable_hours
    return min(
        remaining,
        capacity.current_hour_available,
        capacity.strict_planning_capacity,
        required_this_window,
    )


def _remaining_strict_daily_capacity(
    session: Session,
    task: Task,
    config: dict,
    accounts: list[TgAccount],
    now_value: datetime,
    pacing_stats: PacingStats,
) -> SearchJoinDailyCapacity | None:
    target = (task.type_config or {}).get("daily_click_target_count")
    if not (task.type_config or {}).get("strict_daily_target") or target is None:
        return None
    task_daily_capacity(session, task, pacing_window(task, now_value), 1_000_000, pacing_stats)
    base_budget = int(config.get("max_actions_per_day") or 0)
    effective_budget = pacing_stats.task_daily_effective_budget or base_budget
    remaining_budget = max(0, effective_budget - pacing_stats.task_daily_action_count) if base_budget else None
    capacity_config = {**config, "max_actions_per_day": effective_budget}
    account_capacity = configured_account_source_capacity(
        capacity_config,
        candidate_account_count=len(accounts),
        allow_repeat=bool((task.type_config or {}).get("allow_same_account_repeat_application")),
        keyword_count=len(config.get("keyword_hashes") or []),
        captcha_trigger_rate=float(config.get("captcha_trigger_rate") or 0.0),
    )
    remaining_account_capacity = max(0, account_capacity - pacing_stats.task_daily_action_count)
    timezone = ZoneInfo(task.timezone or "Asia/Shanghai")
    source_now = now_value.replace(tzinfo=BEIJING_TZ) if now_value.tzinfo is None else now_value
    local_now = source_now.astimezone(timezone)
    day_end = local_now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    deadline = task.scheduled_end
    deadline_local = (
        (deadline.replace(tzinfo=BEIJING_TZ) if deadline.tzinfo is None else deadline).astimezone(timezone)
        if deadline
        else None
    )
    active_end = min(day_end, deadline_local) if deadline_local else day_end
    return strict_daily_capacity(
        task.id,
        task.timezone,
        config,
        candidate_account_count=len(accounts),
        account_source_capacity=remaining_account_capacity,
        effective_date=local_now.date(),
        capacity_day_kind="partial_day",
        active_start=local_now,
        active_end=active_end,
        daily_source_budget=remaining_budget,
        occupied_sources_by_hour=hourly_source_occupancy(
            session,
            task,
            pacing_window(task, now_value),
            now_value=now_value,
        ),
        current_hour=local_now.hour,
    )


def _strict_daily_target_is_impossible(task: Task, target_progress, capacity: SearchJoinDailyCapacity | None) -> bool:
    if capacity is None:
        return False
    target = int((task.type_config or {}).get("daily_click_target_count") or 0)
    return (
        int(target_progress.confirmed_count)
        + int(target_progress.held_count)
        + capacity.strict_planning_capacity
        < target
    )


def _record_strict_capacity_blocked(
    task: Task,
    target_progress,
    capacity: SearchJoinDailyCapacity | None,
    pacing_stats: PacingStats,
) -> int:
    if capacity is None:
        raise ValueError("strict capacity is required")
    _record_strict_capacity_snapshot(task, target_progress, capacity, blocked=True)
    stats = dict(task.stats or {})
    search_stats = dict(stats.get("search_join_stats") or {})
    search_stats["pacing_limits"] = pacing_stats.as_dict()
    stats["search_join_stats"] = search_stats
    task.stats = stats
    task.last_error = "daily_target_capacity_insufficient"
    return 0


def _record_strict_capacity_snapshot(
    task: Task,
    target_progress,
    capacity: SearchJoinDailyCapacity | None,
    *,
    blocked: bool = False,
) -> None:
    if capacity is None:
        return
    target = int((task.type_config or {}).get("daily_click_target_count") or 0)
    confirmed = int(getattr(target_progress, "confirmed_count", 0) or 0)
    held = int(getattr(target_progress, "held_count", 0) or 0)
    capacity_feasible = confirmed + held + capacity.strict_planning_capacity >= target
    daily_outcome = "met" if target > 0 and confirmed >= target else "blocked" if blocked else "at_risk"
    stats = dict(task.stats or {})
    search_stats = dict(stats.get("search_join_stats") or {})
    # PRD §2.20.3 RC-6: 产能预判扩展字段，写入 per_account_daily_action_limit、验证码触发率预估、
    # 有效账号数，便于任务详情显示和 blocker 审计。验证码触发率由产品在 pacing_config 配置。
    config = _runtime_config(task)
    per_account_daily_action_limit = int(config.get("per_account_daily_action_limit") or 0)
    search_stats["daily_fulfillment"] = {
        **capacity.as_dict(),
        "daily_click_target_count": target,
        "confirmed_click_count": confirmed,
        "held_click_count": held,
        "remaining_click_slots": int(getattr(target_progress, "remaining_slot_count", 0) or 0),
        "capacity_feasible": capacity_feasible,
        "daily_outcome": daily_outcome,
        "per_account_daily_action_limit": per_account_daily_action_limit,
        "captcha_trigger_rate": capacity.captcha_trigger_rate,
        "effective_account_count": capacity.effective_account_count,
        **({"blocker_code": "daily_target_capacity_insufficient"} if blocked else {}),
    }
    stats["search_join_stats"] = search_stats
    task.stats = stats
    if not blocked and task.last_error == "daily_target_capacity_insufficient":
        task.last_error = ""


def _record_planner_account_selection_warning(
    task: Task,
    configured_account_count: int,
    effective_account_count: int,
) -> None:
    """PRD §2.20.3 RC-6: 实际候选账号数 < 配置候选账号数 50% 时写 planner_account_selection_narrow 告警。"""
    if configured_account_count <= 0:
        return
    narrow = effective_account_count < configured_account_count * 0.5
    stats = dict(task.stats or {})
    search_stats = dict(stats.get("search_join_stats") or {})
    account_selection = {
        "configured_account_count": configured_account_count,
        "effective_account_count": effective_account_count,
        "planner_account_selection_narrow": bool(narrow),
    }
    search_stats["account_selection"] = account_selection
    stats["search_join_stats"] = search_stats
    task.stats = stats


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

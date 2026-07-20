from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AccountProxy, AccountStatus, SearchRankDeboostExemptGroup, Task, TgAccount
from app.models.search_rank_deboost import AccountGroupProxyBinding
from app.security import encrypt_secret
from app.services._common import _now
from app.services.account_usage_policy import apply_rank_deboost_account_filters
from app.services.proxy_airport_accounts import AVAILABLE_NODE_STATUS, EXECUTABLE_PROXY_PROTOCOLS
from app.services.search_rank_deboost_alerts import record_exempt_group_missing_alert
from app.services.task_center.payloads import SearchRankDeboostPayload, create_search_rank_deboost_action
from app.services.task_center.rank_deboost_runtime_authorization import resolve_rank_deboost_runtime_authorization
from app.services.task_center.search_rank_deboost_reservations import (
    release_expired_pending_reservations,
    reserve_click,
)

from ..search_rank_deboost import (
    EXEMPT_GROUP_PENDING_REAL_SEARCH,
    EXEMPT_PLACEHOLDER_USERNAME,
    is_pending_exempt_group,
    validate_rank_deboost_protocol_samples,
)
from ..search_rank_deboost_targets import require_rank_deboost_target_group_refs
from ..search_click_target_progress import reconcile_search_click_target_progress
from ..search_rank_deboost_pacing import (
    DEFAULT_DWELL_SECONDS_MAX,
    DEFAULT_DWELL_SECONDS_MIN,
    DEFAULT_MAX_ACTIONS_PER_HOUR,
    DeboostPacingStats,
    account_click_allowed,
    deboost_pacing_window,
    lock_rank_deboost_quota_scope,
    runtime_search_rank_deboost_config,
)
from ..stats import search_rank_deboost_hourly_execution


MIN_DWELL_SECONDS = 1
MAX_DWELL_SECONDS = 600


class PlanBlock(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class PlanContext:
    config: dict
    account_config: dict
    bot_username: str
    keywords: list[str]
    target_group_ids: list[int]
    target_group_refs: list[dict]
    exempt_group_username: str
    dwell_seconds_min: int
    dwell_seconds_max: int
    max_actions: int


def build_plan(session: Session, task: Task) -> int:
    """搜索排名观察 Planner：校验前置条件并创建 search_rank_deboost action。"""
    _lock_task_for_planning(session, task)
    release_expired_pending_reservations(session, tenant_id=task.tenant_id)
    target_progress = reconcile_search_click_target_progress(session, task)
    if target_progress.completed or target_progress.remaining_slot_count == 0:
        return 0
    try:
        context = _resolve_plan_context(session, task)
    except PlanBlock as exc:
        return _block(task, exc.code, exc.message)

    if target_progress.remaining_slot_count is not None:
        context = replace(context, max_actions=min(context.max_actions, target_progress.remaining_slot_count))
    accounts = _rank_deboost_accounts(session, task.tenant_id, context.account_config)
    if not accounts:
        return _block(task, "account_unavailable", "排名观察专用分组无可用账号")
    try:
        return _create_planned_actions(session, task, context, accounts)
    except PlanBlock as exc:
        return _block(task, exc.code, exc.message)


def _resolve_plan_context(session: Session, task: Task) -> PlanContext:
    config = runtime_search_rank_deboost_config(task)
    bot_username = _first_bot_username(config)
    _validate_protocol_samples(session, task, bot_username)
    keywords = _keyword_items(config)
    if not keywords:
        raise PlanBlock("keyword_missing", "搜索排名观察任务缺少关键词配置")
    target_group_ids = _target_group_ids(config)
    try:
        target_group_refs = require_rank_deboost_target_group_refs(
            session,
            task.tenant_id,
            target_group_ids,
            reference_type=_target_reference_type(config),
        )
    except ValueError as exc:
        raise PlanBlock("target_identity_missing", str(exc)) from exc
    account_config = _rank_account_config(task, config)
    exempt_group_username = _exempt_group_username(session, task.tenant_id, task.id)
    _record_missing_exempt_group(session, task, exempt_group_username)
    dwell_seconds_min, dwell_seconds_max = _dwell_bounds(config)
    return PlanContext(
        config=config,
        account_config=account_config,
        bot_username=bot_username,
        keywords=keywords,
        target_group_ids=target_group_ids,
        target_group_refs=target_group_refs,
        exempt_group_username=exempt_group_username,
        dwell_seconds_min=dwell_seconds_min,
        dwell_seconds_max=dwell_seconds_max,
        max_actions=int(config.get("max_actions_per_hour") or DEFAULT_MAX_ACTIONS_PER_HOUR),
    )


def _dwell_bounds(config: dict) -> tuple[int, int]:
    try:
        raw_minimum = config.get("dwell_seconds_min")
        raw_maximum = config.get("dwell_seconds_max")
        minimum = DEFAULT_DWELL_SECONDS_MIN if raw_minimum is None else int(raw_minimum)
        maximum = DEFAULT_DWELL_SECONDS_MAX if raw_maximum is None else int(raw_maximum)
    except (TypeError, ValueError) as exc:
        raise PlanBlock("invalid_dwell_range", "搜索排名观察停留时间必须是整数") from exc
    if minimum < MIN_DWELL_SECONDS or maximum > MAX_DWELL_SECONDS or maximum < minimum:
        raise PlanBlock(
            "invalid_dwell_range",
            f"dwell_seconds_max 必须在 {MIN_DWELL_SECONDS}..{MAX_DWELL_SECONDS} 且不小于 dwell_seconds_min",
        )
    return minimum, maximum


def _validate_protocol_samples(session: Session, task: Task, bot_username: str) -> None:
    try:
        validate_rank_deboost_protocol_samples(session, task.tenant_id, bot_username)
    except ValueError as exc:
        raise PlanBlock("protocol_sample_missing", str(exc)) from exc


def _target_group_ids(config: dict) -> list[int]:
    target_group_ids = [int(item) for item in config.get("target_group_ids") or [] if item is not None]
    if not target_group_ids:
        raise PlanBlock("target_group_ids_missing", "搜索排名观察任务缺少我方目标群 ID")
    return target_group_ids


def _target_reference_type(config: dict) -> str | None:
    reference_type = str(config.get("target_reference_type") or "").strip()
    return reference_type or None


def _rank_account_config(task: Task, config: dict) -> dict:
    account_config = dict(task.account_config or {})
    if account_config:
        return account_config
    account_pool_id = int(config.get("account_pool_id") or 0)
    if account_pool_id > 0:
        return {"selection_mode": "group", "account_group_id": account_pool_id}
    return {"selection_mode": "all"}


def _matching_binding_for_account(
    session: Session,
    task: Task,
    account: TgAccount,
) -> AccountGroupProxyBinding:
    binding = _active_group_binding(session, task.tenant_id, int(account.pool_id or 0))
    if binding is None:
        raise PlanBlock("group_proxy_binding_missing", "搜索排名观察任务缺少 active 分组级代理绑定")
    _assert_runtime_proxy_ready(session, task.tenant_id, binding)
    return binding


def _assert_runtime_proxy_ready(session: Session, tenant_id: int, binding: AccountGroupProxyBinding) -> None:
    if binding.runtime_proxy_id is None:
        raise PlanBlock("group_proxy_runtime_proxy_missing", "搜索排名观察分组绑定缺少 runtime proxy")
    proxy = session.get(AccountProxy, binding.runtime_proxy_id)
    if proxy is None or proxy.tenant_id != tenant_id:
        raise PlanBlock("group_proxy_runtime_proxy_missing", "搜索排名观察分组绑定缺少 runtime proxy")
    protocol = (proxy.protocol or "").strip().lower()
    host = (proxy.host or "").strip()
    if protocol not in EXECUTABLE_PROXY_PROTOCOLS or not host or int(proxy.port or 0) <= 0 or proxy.status != AVAILABLE_NODE_STATUS:
        raise PlanBlock("group_proxy_runtime_proxy_invalid", "搜索排名观察分组绑定 runtime proxy 不可执行")


def _record_missing_exempt_group(session: Session, task: Task, username: str) -> None:
    if not is_pending_exempt_group(username):
        return
    record_exempt_group_missing_alert(session, tenant_id=task.tenant_id, task_id=task.id)
    raise PlanBlock(EXEMPT_GROUP_PENDING_REAL_SEARCH, "随机豁免群尚未来自真实搜索结果，不能规划搜索排名观察 action")


def _create_planned_actions(
    session: Session,
    task: Task,
    context: PlanContext,
    accounts: list[TgAccount],
) -> int:
    now_value = _now()
    pacing_stats = _pacing_stats(task, now_value)
    blockers: dict[str, int] = {}
    created = 0
    for account in accounts:
        if created >= context.max_actions:
            break
        created += _create_account_action(session, task, context, account, pacing_stats, blockers, now_value)
    hourly = search_rank_deboost_hourly_execution(session, task, now_value)
    _record_hourly(task, hourly, created, blockers, pacing_stats)
    reconcile_search_click_target_progress(session, task)
    task.last_error = ""
    return created


def _create_account_action(
    session: Session,
    task: Task,
    context: PlanContext,
    account: TgAccount,
    pacing_stats: DeboostPacingStats,
    blockers: dict[str, int],
    now_value,
) -> int:
    window = deboost_pacing_window(task, now_value)
    for keyword in context.keywords:
        keyword_hash = _hash_keyword(keyword)
        account_pool_id = int(account.pool_id or 0)
        if not account_click_allowed(session, task, account.id, keyword_hash, account_pool_id, window, pacing_stats):
            _count_blocker(blockers, pacing_stats.last_limit_reason or "pacing_blocked")
            continue
        binding = _matching_binding_for_account(session, task, account)
        payload = _build_payload(context, binding, account_pool_id, keyword_hash, keyword)
        try:
            resolve_rank_deboost_runtime_authorization(session, account, payload)
        except ValueError as exc:
            _count_blocker(blockers, str(exc))
            continue
        action = create_search_rank_deboost_action(session, task, account.id, now_value, payload)
        reserve_click(
            session,
            task=task,
            action=action,
            account=account,
            account_pool_id=account_pool_id,
            keyword_hash=keyword_hash,
            now_value=now_value,
        )
        return 1
    return 0


def _pacing_stats(task: Task, now_value) -> DeboostPacingStats:
    window = deboost_pacing_window(task, now_value)
    return DeboostPacingStats(
        tenant_timezone=task.timezone or "Asia/Shanghai",
        local_date=window.local_date.isoformat(),
    )


def _build_payload(
    context: PlanContext,
    binding: AccountGroupProxyBinding,
    account_pool_id: int,
    keyword_hash: str,
    keyword_text: str,
) -> SearchRankDeboostPayload:
    return SearchRankDeboostPayload(
        bot_username=context.bot_username,
        keyword_hash=keyword_hash,
        keyword_text_ciphertext=encrypt_secret(keyword_text),
        target_group_ids=context.target_group_ids,
        target_group_refs=context.target_group_refs,
        account_pool_id=account_pool_id,
        proxy_airport_node_id=int(binding.proxy_airport_node_id),
        exempt_group_username=context.exempt_group_username,
        dwell_seconds_min=context.dwell_seconds_min,
        dwell_seconds_max=context.dwell_seconds_max,
        runtime_environment={
            "proxy_egress_guard": "verified",
            "group_proxy_binding_id": str(binding.id),
            "runtime_proxy_id": str(binding.runtime_proxy_id),
            "binding_generation": str(binding.binding_generation),
            "proxy_airport_node_id": str(binding.proxy_airport_node_id),
            "account_pool_id": str(account_pool_id),
            "observed_exit_ip": binding.observed_exit_ip or "",
        },
    )


def _lock_task_for_planning(session: Session, task: Task) -> None:
    lock_rank_deboost_quota_scope(session, task)
    session.execute(select(Task.id).where(Task.id == task.id).with_for_update()).scalar_one_or_none()


def _first_bot_username(config: dict) -> str:
    bots = config.get("search_bots") or []
    if bots and isinstance(bots[0], str):
        return str(bots[0]).strip().lstrip("@")
    return "jisou"


def _keyword_items(config: dict) -> list[str]:
    keywords = config.get("keywords") or []
    if not isinstance(keywords, list):
        return []
    return [_keyword_text(item) for item in keywords if _keyword_text(item)]


def _keyword_text(item) -> str:
    if isinstance(item, dict):
        return str(item.get("text") or "").strip()
    return str(item).strip()


def _hash_keyword(text: str) -> str:
    return hashlib.sha256(text.strip().lower().encode("utf-8")).hexdigest()


def _rank_deboost_accounts(session: Session, tenant_id: int, account_config: dict) -> list[TgAccount]:
    stmt = apply_rank_deboost_account_filters(
        select(TgAccount).where(
            TgAccount.tenant_id == tenant_id,
            TgAccount.deleted_at.is_(None),
            TgAccount.status == AccountStatus.ACTIVE.value,
        )
    )
    stmt = _apply_rank_account_selection(stmt, account_config)
    if stmt is None:
        return []
    return list(session.scalars(stmt.order_by(TgAccount.id.asc())))


def _rank_deboost_pool_accounts(session: Session, tenant_id: int, account_pool_id: int) -> list[TgAccount]:
    return _rank_deboost_accounts(
        session,
        tenant_id,
        {"selection_mode": "group", "account_group_id": account_pool_id},
    )


def _apply_rank_account_selection(stmt, account_config: dict):
    mode = str(account_config.get("selection_mode") or "all")
    if mode == "manual":
        account_ids = [int(item) for item in account_config.get("account_ids") or [] if int(item) > 0]
        return stmt.where(TgAccount.id.in_(account_ids)) if account_ids else None
    if mode == "group":
        pool_id = int(account_config.get("account_group_id") or 0)
        return stmt.where(TgAccount.pool_id == pool_id) if pool_id > 0 else None
    return stmt


def _active_group_binding(session: Session, tenant_id: int, account_pool_id: int) -> AccountGroupProxyBinding | None:
    return session.scalar(
        select(AccountGroupProxyBinding).where(
            AccountGroupProxyBinding.tenant_id == tenant_id,
            AccountGroupProxyBinding.account_pool_id == account_pool_id,
            AccountGroupProxyBinding.status == "active",
            AccountGroupProxyBinding.unbound_at.is_(None),
        ).limit(1)
    )


def _exempt_group_username(session: Session, tenant_id: int, task_id: str) -> str:
    record = session.scalar(
        select(SearchRankDeboostExemptGroup).where(
            SearchRankDeboostExemptGroup.tenant_id == tenant_id,
            SearchRankDeboostExemptGroup.task_id == task_id,
        )
    )
    if record is None:
        return EXEMPT_PLACEHOLDER_USERNAME
    return record.exempt_group_username or EXEMPT_PLACEHOLDER_USERNAME


def _count_blocker(blockers: dict[str, int], code: str) -> None:
    blockers[code or "pacing_blocked"] = int(blockers.get(code or "pacing_blocked", 0)) + 1


def _block(task: Task, code: str, message: str) -> int:
    task.last_error = message
    _record_hourly(task, _blocked_hourly(code), 0, {code: 1}, None)
    return 0


def _blocked_hourly(code: str) -> dict:
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


def _record_hourly(
    task: Task,
    hourly: dict,
    planned_count: int,
    blockers: dict,
    pacing_stats: DeboostPacingStats | None,
) -> int:
    stats = dict(task.stats or {})
    deboost_stats = dict(stats.get("search_rank_deboost_stats") or {})
    hourly_execution = dict(hourly)
    hourly_execution["last_planned_count"] = planned_count
    hourly_execution["last_blockers"] = dict(blockers)
    deboost_stats["hourly_execution"] = hourly_execution
    if pacing_stats is not None:
        deboost_stats["pacing_limits"] = pacing_stats.as_dict()
    stats["search_rank_deboost_stats"] = deboost_stats
    task.stats = stats
    return planned_count


__all__ = ["build_plan"]

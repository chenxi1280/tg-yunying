from __future__ import annotations

import hashlib
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import AccountStatus, SearchRankDeboostExemptGroup, Task, TgAccount
from app.models.search_rank_deboost import AccountGroupProxyBinding
from app.services._common import _now
from app.services.search_rank_deboost_alerts import record_exempt_group_missing_alert
from app.services.task_center.payloads import SearchRankDeboostPayload, create_search_rank_deboost_action

from ..search_rank_deboost import (
    EXEMPT_GROUP_PENDING_REAL_SEARCH,
    EXEMPT_PLACEHOLDER_USERNAME,
    RANK_DEBOOST_GRADUATION_ACCOUNT_LIMIT,
    is_pending_exempt_group,
    validate_rank_deboost_protocol_samples,
)
from ..search_rank_deboost_pacing import (
    DEFAULT_DWELL_SECONDS_MAX,
    DEFAULT_DWELL_SECONDS_MIN,
    DEFAULT_MAX_ACTIONS_PER_HOUR,
    DeboostPacingStats,
    account_click_allowed,
    deboost_pacing_window,
    runtime_search_rank_deboost_config,
)
from ..stats import search_rank_deboost_hourly_execution


class PlanBlock(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class PlanContext:
    config: dict
    bot_username: str
    keywords: list[str]
    target_group_ids: list[int]
    account_pool_id: int
    proxy_airport_node_id: int
    binding: AccountGroupProxyBinding
    exempt_group_username: str
    dwell_seconds_min: int
    dwell_seconds_max: int
    max_actions: int


def build_plan(session: Session, task: Task) -> int:
    """搜索排名观察 Planner：校验前置条件并创建 search_rank_deboost action。"""
    _lock_task_for_planning(session, task)
    try:
        context = _resolve_plan_context(session, task)
    except PlanBlock as exc:
        return _block(task, exc.code, exc.message)

    accounts = _rank_deboost_pool_accounts(session, task.tenant_id, context.account_pool_id)
    if not accounts:
        return _block(task, "account_unavailable", "排名观察专用分组无可用账号")
    return _create_planned_actions(session, task, context, accounts)


def _resolve_plan_context(session: Session, task: Task) -> PlanContext:
    config = runtime_search_rank_deboost_config(task)
    bot_username = _first_bot_username(config)
    _validate_protocol_samples(session, task, bot_username)
    keywords = _keyword_items(config)
    if not keywords:
        raise PlanBlock("keyword_missing", "搜索排名观察任务缺少关键词配置")
    target_group_ids = _target_group_ids(config)
    account_pool_id, proxy_airport_node_id = _binding_ids(config)
    binding = _matching_binding(session, task, account_pool_id, proxy_airport_node_id)
    _validate_account_limit(session, task, account_pool_id)
    exempt_group_username = _exempt_group_username(session, task.tenant_id, task.id)
    _record_missing_exempt_group(session, task, exempt_group_username)
    return PlanContext(
        config=config,
        bot_username=bot_username,
        keywords=keywords,
        target_group_ids=target_group_ids,
        account_pool_id=account_pool_id,
        proxy_airport_node_id=proxy_airport_node_id,
        binding=binding,
        exempt_group_username=exempt_group_username,
        dwell_seconds_min=int(config.get("dwell_seconds_min") or DEFAULT_DWELL_SECONDS_MIN),
        dwell_seconds_max=int(config.get("dwell_seconds_max") or DEFAULT_DWELL_SECONDS_MAX),
        max_actions=int(config.get("max_actions_per_hour") or DEFAULT_MAX_ACTIONS_PER_HOUR),
    )


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


def _binding_ids(config: dict) -> tuple[int, int]:
    account_pool_id = int(config.get("account_pool_id") or 0)
    proxy_airport_node_id = int(config.get("proxy_airport_node_id") or 0)
    if account_pool_id <= 0 or proxy_airport_node_id <= 0:
        raise PlanBlock("binding_missing", "搜索排名观察任务缺少账号分组或代理节点配置")
    return account_pool_id, proxy_airport_node_id


def _matching_binding(
    session: Session,
    task: Task,
    account_pool_id: int,
    proxy_airport_node_id: int,
) -> AccountGroupProxyBinding:
    binding = _active_group_binding(session, task.tenant_id, account_pool_id)
    if binding is None or binding.proxy_airport_node_id != proxy_airport_node_id:
        raise PlanBlock("group_proxy_binding_missing", "搜索排名观察任务缺少 active 分组级代理绑定")
    return binding


def _validate_account_limit(session: Session, task: Task, account_pool_id: int) -> None:
    account_count = session.scalar(
        select(func.count(TgAccount.id)).where(
            TgAccount.tenant_id == task.tenant_id,
            TgAccount.pool_id == account_pool_id,
        )
    ) or 0
    if account_count > RANK_DEBOOST_GRADUATION_ACCOUNT_LIMIT:
        raise PlanBlock(
            "graduation_account_limit_exceeded",
            f"rank_deboost 分组 {account_pool_id} 灰度账号数 {account_count} 超过上限 "
            f"{RANK_DEBOOST_GRADUATION_ACCOUNT_LIMIT}",
        )


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
        if not account_click_allowed(session, task, account.id, keyword_hash, context.account_pool_id, window, pacing_stats):
            _count_blocker(blockers, pacing_stats.last_limit_reason or "pacing_blocked")
            continue
        payload = _build_payload(context, keyword_hash, keyword)
        create_search_rank_deboost_action(session, task, account.id, now_value, payload)
        return 1
    return 0


def _pacing_stats(task: Task, now_value) -> DeboostPacingStats:
    window = deboost_pacing_window(task, now_value)
    return DeboostPacingStats(
        tenant_timezone=task.timezone or "Asia/Shanghai",
        local_date=window.local_date.isoformat(),
    )


def _build_payload(context: PlanContext, keyword_hash: str, keyword_text: str) -> SearchRankDeboostPayload:
    return SearchRankDeboostPayload(
        bot_username=context.bot_username,
        keyword_hash=keyword_hash,
        keyword_text_ciphertext=keyword_text,
        target_group_ids=context.target_group_ids,
        account_pool_id=context.account_pool_id,
        proxy_airport_node_id=context.proxy_airport_node_id,
        exempt_group_username=context.exempt_group_username,
        dwell_seconds_min=context.dwell_seconds_min,
        dwell_seconds_max=context.dwell_seconds_max,
        runtime_environment={
            "proxy_egress_guard": "verified",
            "group_proxy_binding_id": str(context.binding.id),
            "proxy_airport_node_id": str(context.proxy_airport_node_id),
            "account_pool_id": str(context.account_pool_id),
            "observed_exit_ip": context.binding.observed_exit_ip or "",
        },
    )


def _lock_task_for_planning(session: Session, task: Task) -> None:
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


def _rank_deboost_pool_accounts(session: Session, tenant_id: int, account_pool_id: int) -> list[TgAccount]:
    return list(
        session.scalars(
            select(TgAccount).where(
                TgAccount.tenant_id == tenant_id,
                TgAccount.pool_id == account_pool_id,
                TgAccount.deleted_at.is_(None),
                TgAccount.status == AccountStatus.ACTIVE.value,
                TgAccount.account_identity == "rank_deboost",
            ).order_by(TgAccount.id.asc())
        )
    )


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

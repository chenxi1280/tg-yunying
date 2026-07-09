from __future__ import annotations

from datetime import datetime
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    AccountProxyBinding,
    AccountPool,
    BotProtocolSample,
    ProxyAirportNode,
    SearchRankDeboostExemptGroup,
    Task,
    TgAccount,
)
from app.models.enums import AccountProxyBindingScope
from app.services._common import _now, audit


EXEMPT_PLACEHOLDER_USERNAME = "pending_real_search"
EXEMPT_GROUP_PENDING_REAL_SEARCH = "exempt_group_pending_real_search"
RANK_OBSERVATION_GATEWAY_UNAVAILABLE = "rank_observation_gateway_unavailable"
TARGET_NOT_IN_RESULTS = "target_not_in_results"
ALL_EXEMPT_CLICKS = "all_exempt_clicks"

# 首版灰度账号数硬上限：与 PRODUCTION_RUNTIME.md 灰度发布约束一致，调整时需同步文档。
RANK_DEBOOST_GRADUATION_ACCOUNT_LIMIT = 10


@dataclass(frozen=True)
class ExemptGroupSelection:
    username: str
    peer_id: str
    title: str
    match_strategy: str = "username"


def compute_deboost_click_targets(
    search_results: list[dict],
    my_target_ids: list[int],
    exempt_group_username: str,
) -> dict:
    """实时判定点击范围。

    点击范围 = 当前搜索结果中除「我方目标群 + 任务级预选随机豁免群 + 排名比我方目标群更低的群」之外的所有竞争群。

    Args:
        search_results: 搜索结果列表，每项含 position/username/peer_id/title 等字段，按 position 升序。
        my_target_ids: 我方目标群 ID 列表（可为 int ID 或 str username/peer_id）。
        exempt_group_username: 任务级预选随机豁免群的 username。

    Returns:
        dict: click_targets / my_target_position / exempt_position / skipped_reason。
    """
    target_position = _find_target_position(search_results, my_target_ids)
    if target_position is None:
        return {
            "click_targets": [],
            "my_target_position": None,
            "exempt_position": None,
            "skipped_reason": TARGET_NOT_IN_RESULTS,
        }

    exempt_position = _find_position_by_username(search_results, exempt_group_username)

    click_targets: list[dict] = []
    for item in search_results:
        position = _position_of(item)
        if position is None:
            continue
        if position == target_position:
            continue
        if exempt_position is not None and position == exempt_position:
            continue
        if position > target_position:
            continue
        click_targets.append(item)

    skipped_reason = ALL_EXEMPT_CLICKS if not click_targets else None
    return {
        "click_targets": click_targets,
        "my_target_position": target_position,
        "exempt_position": exempt_position,
        "skipped_reason": skipped_reason,
    }


def _find_target_position(search_results: list[dict], my_target_ids: list[int]) -> int | None:
    if not my_target_ids:
        return None
    target_tokens = {str(item).strip().lstrip("@") for item in my_target_ids if item is not None}
    target_tokens.discard("")
    if not target_tokens:
        return None
    for item in search_results:
        position = _position_of(item)
        if position is None:
            continue
        for field in ("id", "peer_id", "username"):
            value = item.get(field)
            if value is None:
                continue
            token = str(value).strip().lstrip("@")
            if token and token in target_tokens:
                return position
    return None


def _find_position_by_username(search_results: list[dict], username: str) -> int | None:
    token = (username or "").strip().lstrip("@")
    if not token:
        return None
    for item in search_results:
        item_username = str(item.get("username") or "").strip().lstrip("@")
        if item_username and item_username == token:
            return _position_of(item)
    return None


def _position_of(item: dict) -> int | None:
    position = item.get("position")
    if position is None:
        return None
    try:
        return int(position)
    except (TypeError, ValueError):
        return None


def validate_rank_deboost_protocol_samples(
    session: Session, tenant_id: int, bot_username: str
) -> None:
    """协议样本采集门槛校验。

    校验 bot_protocol_samples 中 sample_purpose='rank_deboost'、bot_username=bot_username、
    is_active=true 的样本是否达阈值。未通过时 raise ValueError，错误消息含缺口详情。

    阈值（spec 定义）：
    - /start 响应样本 ≥ 2 个账号（sample_type='start_response'）
    - 关键词搜索响应样本 ≥ 5 个关键词（sample_type='search_results'）
    - 翻页响应样本 ≥ 3 次分页（sample_type='pagination_response'）
    - 竞争群结果项按钮结构样本 ≥ 3 种 button effect 类型（sample_type='button_structure'，
      从 structure_json 提取 button_effect 字段去重计数）
    - 出口防泄漏样本 ≥ 3 次（sample_type='exit_ip_observation'）
    """
    if not bot_username:
        raise ValueError("协议样本校验需要 bot_username")
    gaps = _protocol_sample_gaps(_rank_deboost_protocol_samples(session, tenant_id, bot_username))
    if gaps:
        raise ValueError(f"协议样本不足：{', '.join(gaps)}")


def _rank_deboost_protocol_samples(session: Session, tenant_id: int, bot_username: str) -> list[BotProtocolSample]:
    return list(session.scalars(select(BotProtocolSample).where(
        BotProtocolSample.tenant_id == tenant_id,
        BotProtocolSample.bot_username == bot_username,
        BotProtocolSample.sample_purpose == "rank_deboost",
        BotProtocolSample.is_active.is_(True),
    )))


def _protocol_sample_gaps(samples: list[BotProtocolSample]) -> list[str]:
    gaps: list[str] = []
    start_response_count = sum(1 for s in samples if s.sample_type == "start_response")
    if start_response_count < 2:
        gaps.append(f"start_response {start_response_count}/2")
    search_results_count = sum(1 for s in samples if s.sample_type == "search_results")
    if search_results_count < 5:
        gaps.append(f"search_results {search_results_count}/5")
    pagination_count = sum(1 for s in samples if s.sample_type == "pagination_response")
    if pagination_count < 3:
        gaps.append(f"pagination_response {pagination_count}/3")
    gaps.extend(_protocol_button_effect_gaps(samples))
    exit_ip_count = sum(1 for s in samples if s.sample_type == "exit_ip_observation")
    if exit_ip_count < 3:
        gaps.append(f"exit_ip_observation {exit_ip_count}/3")
    return gaps


def _protocol_button_effect_gaps(samples: list[BotProtocolSample]) -> list[str]:
    button_effects: set[str] = set()
    for s in samples:
        if s.sample_type != "button_structure":
            continue
        for effect in _extract_button_effects(s.structure_json):
            button_effects.add(effect)
    if len(button_effects) < 3:
        return [f"button_structure effect_types {len(button_effects)}/3"]
    return []


def _extract_button_effects(structure_json: dict | None) -> list[str]:
    """从 structure_json 提取 button_effect 值列表。

    支持两种结构：
    - 顶层 button_effect 字段（单个按钮）
    - buttons 列表，每项含 button_effect 字段
    """
    if not structure_json:
        return []
    effects: list[str] = []
    effect = structure_json.get("button_effect")
    if isinstance(effect, str) and effect:
        effects.append(effect)
    buttons = structure_json.get("buttons")
    if isinstance(buttons, list):
        for btn in buttons:
            if isinstance(btn, dict):
                btn_effect = btn.get("button_effect")
                if isinstance(btn_effect, str) and btn_effect:
                    effects.append(btn_effect)
    return effects


def preselect_exempt_group(
    session: Session,
    *,
    tenant_id: int,
    task_id: str,
    operator: str,
    my_target_ids: list[int],
    search_results: list[dict] | None = None,
) -> SearchRankDeboostExemptGroup:
    """从搜索结果中随机选取 1 个非我方目标群作为豁免群并写入持久化。

    本任务不实现真实 Telegram 搜索逻辑（Task 13 Executor 工作）。当前实现：
    - 若 search_results 为空，写入占位豁免群（username=pending_real_search），启动和 Planner 会阻塞。
    - 若 search_results 非空，从非我方目标群中随机选 1 个写入。

    函数签名与写表逻辑已完整，Task 13 只需替换搜索调用即可。
    """
    existing = _existing_exempt_group(session, tenant_id, task_id)
    selection = _select_exempt_group(my_target_ids, search_results)
    now = _now()
    if existing is None:
        record = _new_exempt_group(tenant_id, task_id, operator, selection, now)
        session.add(record)
    else:
        record = _update_exempt_group(existing, operator, selection, now)
    session.flush()
    return record


def is_pending_exempt_group(username: str | None) -> bool:
    normalized = (username or "").strip().lstrip("@")
    return not normalized or normalized == EXEMPT_PLACEHOLDER_USERNAME


def require_real_exempt_group(session: Session, *, tenant_id: int, task_id: str) -> SearchRankDeboostExemptGroup:
    record = _existing_exempt_group(session, tenant_id, task_id)
    if record is None or is_pending_exempt_group(record.exempt_group_username):
        raise ValueError("随机豁免群尚未来自真实搜索结果，请先接入 gateway 并重选真实豁免群")
    return record


def require_rank_observation_gateway(gateway_client: Any | None = None) -> None:
    client = gateway_client if gateway_client is not None else _rank_observation_gateway()
    if not callable(getattr(client, "execute_search_rank_deboost", None)):
        raise ValueError("搜索排名观察 gateway 未接入，不能启动真实任务")


def _rank_observation_gateway() -> Any:
    from app.services._common import gateway

    return gateway


def _existing_exempt_group(session: Session, tenant_id: int, task_id: str) -> SearchRankDeboostExemptGroup | None:
    return session.scalar(select(SearchRankDeboostExemptGroup).where(
        SearchRankDeboostExemptGroup.tenant_id == tenant_id,
        SearchRankDeboostExemptGroup.task_id == task_id,
    ))


def _select_exempt_group(my_target_ids: list[int], search_results: list[dict] | None) -> ExemptGroupSelection:
    import random

    if not search_results:
        return ExemptGroupSelection(username=EXEMPT_PLACEHOLDER_USERNAME, peer_id="", title="")
    target_tokens = {str(item).strip().lstrip("@") for item in my_target_ids if item is not None}
    target_tokens.discard("")
    candidates = [item for item in search_results if not _is_target_item(item, target_tokens)]
    if not candidates:
        return ExemptGroupSelection(username=EXEMPT_PLACEHOLDER_USERNAME, peer_id="", title="")
    chosen = random.choice(candidates)
    return ExemptGroupSelection(
        username=str(chosen.get("username") or "").strip().lstrip("@") or EXEMPT_PLACEHOLDER_USERNAME,
        peer_id=str(chosen.get("peer_id") or ""),
        title=str(chosen.get("title") or ""),
    )


def _new_exempt_group(
    tenant_id: int,
    task_id: str,
    operator: str,
    selection: ExemptGroupSelection,
    selected_at: datetime,
) -> SearchRankDeboostExemptGroup:
    return SearchRankDeboostExemptGroup(
        id=str(uuid4()),
        tenant_id=tenant_id,
        task_id=task_id,
        exempt_group_username=selection.username,
        exempt_group_peer_id=selection.peer_id,
        exempt_group_title=selection.title,
        exempt_group_match_strategy=selection.match_strategy,
        selected_at=selected_at,
        selected_by=operator,
    )


def _update_exempt_group(
    record: SearchRankDeboostExemptGroup,
    operator: str,
    selection: ExemptGroupSelection,
    selected_at: datetime,
) -> SearchRankDeboostExemptGroup:
    record.previous_exempt_group_username = record.exempt_group_username
    record.previous_exempt_group_peer_id = record.exempt_group_peer_id
    record.exempt_group_username = selection.username
    record.exempt_group_peer_id = selection.peer_id
    record.exempt_group_title = selection.title
    record.exempt_group_match_strategy = selection.match_strategy
    record.selected_at = selected_at
    record.selected_by = operator
    return record


def _is_target_item(item: dict, target_tokens: set[str]) -> bool:
    if not target_tokens:
        return False
    for field in ("id", "peer_id", "username"):
        value = item.get(field)
        if value is None:
            continue
        token = str(value).strip().lstrip("@")
        if token and token in target_tokens:
            return True
    return False


def assert_node_available_for_group_binding(
    session: Session,
    *,
    tenant_id: int,
    proxy_airport_node_id: int,
) -> ProxyAirportNode:
    """校验节点存在、健康且未被授权槽位级绑定占用。"""
    node = session.get(ProxyAirportNode, int(proxy_airport_node_id))
    if node is None or node.tenant_id != tenant_id:
        raise ValueError("proxy_airport_node 不存在")
    if node.status != "healthy":
        raise ValueError(f"proxy_airport_node {proxy_airport_node_id} 不可用（status={node.status}）")
    slot_binding = session.scalar(
        select(AccountProxyBinding.id).where(
            AccountProxyBinding.tenant_id == tenant_id,
            AccountProxyBinding.proxy_airport_node_id == node.id,
            AccountProxyBinding.status == "active",
            AccountProxyBinding.unbound_at.is_(None),
        ).limit(1)
    )
    if slot_binding is not None:
        raise ValueError(
            f"节点 {proxy_airport_node_id} 已被授权槽位级绑定占用，不可同时用于排名观察分组"
        )
    return node


def assert_account_pool_for_rank_deboost(
    session: Session,
    *,
    tenant_id: int,
    account_pool_id: int,
) -> AccountPool:
    """校验账号分组存在且 pool_purpose=rank_deboost。"""
    pool = session.get(AccountPool, int(account_pool_id))
    if pool is None or pool.tenant_id != tenant_id:
        raise ValueError("account_pool 不存在")
    if pool.pool_purpose != "rank_deboost":
        raise ValueError(
            f"账号分组 {account_pool_id} pool_purpose={pool.pool_purpose}，必须为 rank_deboost"
        )
    return pool


def validate_rank_deboost_preconditions(
    session: Session,
    *,
    tenant_id: int,
    account_pool_id: int,
    proxy_airport_node_id: int,
    target_group_ids: list[int],
    bot_username: str = "jisou",
) -> None:
    """预检：分组级代理绑定、节点健康、节点容量、随机豁免群、协议样本、灰度账号数。

    任意一项失败 raise ValueError，错误消息含失败原因。被 service 层
    ``create_search_rank_deboost_task`` 调用，集中所有创建前置校验。
    """
    # 1. 校验 account_pool_id 对应分组 pool_purpose='rank_deboost'
    assert_account_pool_for_rank_deboost(
        session,
        tenant_id=tenant_id,
        account_pool_id=account_pool_id,
    )

    # 2 & 3. 校验节点存在、健康、未被授权槽位级 account_proxy_bindings 绑定（节点独占）
    node = assert_node_available_for_group_binding(
        session,
        tenant_id=tenant_id,
        proxy_airport_node_id=proxy_airport_node_id,
    )
    # 节点健康分硬约束：health_score >= 60（当前 ProxyAirportNode 模型未暴露该字段时默认通过，
    # 由 status='healthy' 兜底；后续模型扩展后此校验自动生效）
    health_score = getattr(node, "health_score", None)
    if health_score is not None and float(health_score) < 60:
        raise ValueError(
            f"proxy_airport_node {proxy_airport_node_id} health_score={health_score} 低于 60"
        )

    # 4. 校验协议样本采集（未达阈值 raise ValueError，含缺口详情）
    validate_rank_deboost_protocol_samples(session, tenant_id, bot_username)

    # 5. 校验灰度账号数 ≤ RANK_DEBOOST_GRADUATION_ACCOUNT_LIMIT（从 account_pool 数账号）
    account_count = session.scalar(
        select(func.count(TgAccount.id)).where(
            TgAccount.tenant_id == tenant_id,
            TgAccount.pool_id == int(account_pool_id),
        )
    ) or 0
    if account_count > RANK_DEBOOST_GRADUATION_ACCOUNT_LIMIT:
        raise ValueError(
            f"rank_deboost 排名观察分组 {account_pool_id} 灰度账号数 {account_count} 超过上限 "
            f"{RANK_DEBOOST_GRADUATION_ACCOUNT_LIMIT}"
        )


def to_exempt_group_response(record: SearchRankDeboostExemptGroup) -> dict[str, Any]:
    return {
        "task_id": record.task_id,
        "exempt_group_username": record.exempt_group_username,
        "exempt_group_peer_id": record.exempt_group_peer_id,
        "exempt_group_title": record.exempt_group_title,
        "exempt_group_match_strategy": record.exempt_group_match_strategy,
        "selected_at": record.selected_at,
        "selected_by": record.selected_by,
    }


__all__ = [
    "ALL_EXEMPT_CLICKS",
    "EXEMPT_GROUP_PENDING_REAL_SEARCH",
    "EXEMPT_PLACEHOLDER_USERNAME",
    "RANK_DEBOOST_GRADUATION_ACCOUNT_LIMIT",
    "RANK_OBSERVATION_GATEWAY_UNAVAILABLE",
    "TARGET_NOT_IN_RESULTS",
    "assert_account_pool_for_rank_deboost",
    "assert_node_available_for_group_binding",
    "compute_deboost_click_targets",
    "is_pending_exempt_group",
    "preselect_exempt_group",
    "require_rank_observation_gateway",
    "require_real_exempt_group",
    "to_exempt_group_response",
    "validate_rank_deboost_preconditions",
    "validate_rank_deboost_protocol_samples",
]

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AccountPool
from app.services.account_usage_policy import pool_markers_consistent


NORMAL_SEARCH_CLICK_TASK = "search_join_group"
RANK_SEARCH_CLICK_TASK = "search_rank_deboost"


def require_search_click_account_group(
    session: Session,
    tenant_id: int,
    task_type: str,
    account_group_id: int,
) -> None:
    pool = session.scalar(
        select(AccountPool).where(
            AccountPool.id == account_group_id,
            AccountPool.tenant_id == tenant_id,
            AccountPool.is_enabled.is_(True),
        )
    )
    if pool is None:
        raise ValueError("所选账号组不存在、未启用或不属于当前租户")
    required_purpose = "rank_deboost" if task_type == RANK_SEARCH_CLICK_TASK else "normal"
    if pool.pool_purpose != required_purpose or not pool_markers_consistent(required_purpose, pool.system_key):
        raise ValueError(_account_group_error(task_type))


def search_click_account_config(account_group_id: int) -> dict[str, Any]:
    return {"selection_mode": "group", "account_group_id": account_group_id, "account_ids": []}


def search_click_pacing_config(payload: Any) -> dict[str, Any]:
    config = {
        "max_actions_per_day": payload.max_actions_per_day,
        "daily_jitter_percent": payload.daily_jitter_percent,
        "hourly_jitter_percent": payload.hourly_jitter_percent,
    }
    if hasattr(payload, "per_account_daily_action_limit"):
        config["per_account_daily_action_limit"] = payload.per_account_daily_action_limit
    if payload.quiet_hours is not None:
        config["quiet_hours"] = payload.quiet_hours.model_dump(mode="json")
    return config


def _account_group_error(task_type: str) -> str:
    if task_type == NORMAL_SEARCH_CLICK_TASK:
        return "普通搜索点击任务只能使用普通账号组"
    return "黑搜索点击任务只能使用可用黑账号组"


__all__ = [
    "NORMAL_SEARCH_CLICK_TASK",
    "RANK_SEARCH_CLICK_TASK",
    "require_search_click_account_group",
    "search_click_account_config",
    "search_click_pacing_config",
]

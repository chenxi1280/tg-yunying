from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Task
from app.models.search_rank_deboost import AccountGroupProxyBinding


def binding_snapshot(session: Session, binding: AccountGroupProxyBinding) -> dict:
    return {
        "id": binding.id,
        "tenant_id": binding.tenant_id,
        "account_pool_id": binding.account_pool_id,
        "proxy_airport_node_id": binding.proxy_airport_node_id,
        "runtime_proxy_id": binding.runtime_proxy_id,
        "binding_generation": binding.binding_generation,
        "status": binding.status,
        "observed_exit_ip": binding.observed_exit_ip,
        "observed_exit_country": binding.observed_exit_country,
        "observed_exit_asn": binding.observed_exit_asn,
        "observed_exit_isp": binding.observed_exit_isp,
        "last_probe_at": binding.last_probe_at,
        "last_probe_error": binding.last_probe_error,
        "reference_count": rank_deboost_pool_reference_count(session, binding.tenant_id, binding.account_pool_id),
    }


def rank_deboost_pool_reference_count(session: Session, tenant_id: int, account_pool_id: int) -> int:
    tasks = session.scalars(
        select(Task).where(
            Task.tenant_id == tenant_id,
            Task.type == "search_rank_deboost",
            Task.status.in_(("running", "paused")),
            Task.deleted_at.is_(None),
        )
    )
    return sum(1 for task in tasks if _task_references_pool(task, account_pool_id))


def _task_references_pool(task: Task, account_pool_id: int) -> bool:
    configs = (task.account_config or {}, task.type_config or {})
    keys = ("account_group_id", "account_pool_id", "pool_id")
    return any(_config_pool_id(config, key) == account_pool_id for config in configs for key in keys)


def _config_pool_id(config: dict, key: str) -> int | None:
    value = config.get(key)
    return int(value) if value is not None else None


__all__ = ["binding_snapshot", "rank_deboost_pool_reference_count"]

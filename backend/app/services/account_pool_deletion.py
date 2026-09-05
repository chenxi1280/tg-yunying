"""Retain groups referenced by current bindings or unsettled execution."""
from sqlalchemy import or_, select

from app.models import (
    AccountGroupProxyBinding, AccountPoolConcurrencyLease, Action, ExecutionAttempt,
    Task, TaskAccountGroupBindingSetRevision,
)


CURRENT_TASK_STATES = ("draft", "pending", "running", "paused")
OPEN_ACTION_STATES = ("pending", "claiming", "executing", "retryable_failed", "unknown_after_send")


def assert_pool_can_be_deleted(session, pool):
    _assert_no_proxy_binding(session, pool)
    _assert_no_formal_binding(session, pool)
    _assert_no_unsettled_lease(session, pool)
    tasks = session.scalars(select(Task).where(Task.tenant_id == pool.tenant_id,
        Task.status.in_(("running", "paused"))))
    if any(_task_references_pool(task, pool.id) for task in tasks):
        raise ValueError("账号组仍被 running/paused 任务引用，不能删除")


def _assert_no_proxy_binding(session, pool):
    binding_id = session.scalar(select(AccountGroupProxyBinding.id).where(
        AccountGroupProxyBinding.tenant_id == pool.tenant_id,
        AccountGroupProxyBinding.account_pool_id == pool.id,
        AccountGroupProxyBinding.status == "active",
        AccountGroupProxyBinding.unbound_at.is_(None)))
    if binding_id:
        raise ValueError("账号组存在 active 分组绑定，不能删除")


def _assert_no_formal_binding(session, pool):
    binding = TaskAccountGroupBindingSetRevision
    current = (binding.state.in_(("active", "scheduled"))
        & (binding.task_lifecycle_epoch == Task.task_lifecycle_epoch)
        & Task.status.in_(CURRENT_TASK_STATES) & Task.deleted_at.is_(None))
    unfinished = select(Action.id).where(Action.task_id == binding.task_id,
        Action.tenant_id == binding.tenant_id,
        Action.task_lifecycle_epoch == binding.task_lifecycle_epoch,
        or_(Action.status.in_(OPEN_ACTION_STATES), select(ExecutionAttempt.id).where(
            ExecutionAttempt.action_id == Action.id,
            ExecutionAttempt.status == "result_unknown").exists())).exists()
    groups = session.scalars(select(binding.account_group_ids).join(Task,
        Task.id == binding.task_id).where(binding.tenant_id == pool.tenant_id,
            Task.tenant_id == pool.tenant_id, or_(current, unfinished)))
    if any(pool.id in ids for ids in groups):
        raise ValueError("account_group_current_or_unsettled_binding")


def _assert_no_unsettled_lease(session, pool):
    from .task_center.engagement_runtime_domains import ACTIVE_DOMAIN_LEASE_STATES

    lease = session.scalar(select(AccountPoolConcurrencyLease.id).where(
        AccountPoolConcurrencyLease.tenant_id == pool.tenant_id,
        AccountPoolConcurrencyLease.account_pool_id == pool.id,
        AccountPoolConcurrencyLease.state.in_(ACTIVE_DOMAIN_LEASE_STATES)).limit(1))
    if lease:
        raise ValueError("account_group_unsettled_invocation")


def _task_references_pool(task, pool_id):
    configs = (task.account_config or {}, task.type_config or {})
    keys = ("account_group_id", "account_pool_id", "pool_id")
    single = any(_config_pool_id(config, key) == pool_id for config in configs for key in keys)
    return single or any(pool_id in [int(item) for item in config.get("account_group_ids", [])]
        for config in configs)


def _config_pool_id(config, key):
    value = config.get(key)
    return int(value) if value is not None else None

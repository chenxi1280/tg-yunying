"""Audited configuration-only cutover; existing execution identities are immutable here."""
from dataclasses import dataclass
import hashlib
import json

from sqlalchemy import select

from app.models import Action, Task
from app.search_transport import DIRECT_SEARCH_TRANSPORT_VERSION, LEGACY_SEARCH_TRANSPORT_VERSION
from app.services._common import audit

SEARCH_TYPES = frozenset({"search_click", "search_rank_deboost"})
ACTION_READ_BATCH_SIZE = 100


@dataclass(frozen=True)
class SearchDirectMigration:
    tenant_id: int
    task_ids: tuple[str, ...]
    actor: str
    reference: str

    def validate(self):
        if not self.task_ids or len(set(self.task_ids)) != len(self.task_ids):
            raise ValueError("search_direct_migration_exact_unique_tasks_required")
        if self.tenant_id <= 0 or not self.actor.strip() or not self.reference.strip():
            raise ValueError("search_direct_migration_audit_scope_required")


def preview_search_direct_migration(session, scope: SearchDirectMigration) -> dict:
    scope.validate()
    tasks = _tasks(session, scope, lock=False)
    return {task.id: _preview(session, task, lock=False) for task in tasks}


def apply_search_direct_migration(session, scope: SearchDirectMigration, expected: dict) -> dict:
    scope.validate()
    if set(expected) != set(scope.task_ids):
        raise ValueError("search_direct_migration_preview_scope_mismatch")
    tasks = _tasks(session, scope, lock=True)
    snapshots = {task.id: _preview(session, task, lock=True) for task in tasks}
    for task in tasks:
        if snapshots[task.id]["snapshot_hash"] != expected[task.id]["snapshot_hash"]:
            raise ValueError("search_direct_migration_stale_preview")
        if snapshots[task.id]["old_version"] != LEGACY_SEARCH_TRANSPORT_VERSION:
            raise ValueError("search_direct_migration_legacy_contract_required")
    for task in tasks:
        _apply_task(session, task, scope=scope, before=snapshots[task.id])
    session.flush()
    after = {task.id: _preview(session, task, lock=False) for task in tasks}
    for task in tasks:
        if snapshots[task.id]["protected_hash"] != after[task.id]["protected_hash"]:
            raise ValueError("search_direct_migration_protected_state_changed")
    return after


def _tasks(session, scope, *, lock):
    query = select(Task).where(Task.tenant_id == scope.tenant_id, Task.id.in_(scope.task_ids),
                               Task.deleted_at.is_(None), Task.type.in_(SEARCH_TYPES)).order_by(Task.id)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    tasks = list(session.scalars(query))
    if {task.id for task in tasks} != set(scope.task_ids):
        raise ValueError("search_direct_migration_task_scope_mismatch")
    return tasks


def _preview(session, task, *, lock):
    config = dict(task.type_config or {})
    actions = select(Action).where(Action.task_id == task.id).order_by(Action.id)
    if lock:
        actions = actions.with_for_update().execution_options(populate_existing=True)
    execution = _execution_summary(session, actions)
    protected = {
        "task_id": task.id, "tenant_id": task.tenant_id, "type": task.type, "status": task.status,
        "account_config": task.account_config, "pacing_config": task.pacing_config,
        "failure_policy": task.failure_policy, "scheduled_start": task.scheduled_start,
        "scheduled_end": task.scheduled_end, "timezone": task.timezone,
        "fulfillment_contract_version": task.fulfillment_contract_version, "stats": task.stats,
        "actions": execution,
    }
    return {
        "snapshot_hash": _hash({"config": config, "protected": protected}),
        "protected_hash": _hash(protected), "config_hash": _hash(config),
        "old_version": config.get("transport_contract_version", LEGACY_SEARCH_TRANSPORT_VERSION),
        "action_count": execution["count"], "unknown_action_ids": execution["unknown_action_ids"],
    }


def _execution_summary(session, query):
    digest, count, unknown = hashlib.sha256(), 0, []
    for row in session.scalars(query.execution_options(yield_per=ACTION_READ_BATCH_SIZE)):
        digest.update(_hash(_action_snapshot(row)).encode())
        count += 1
        if row.status == "unknown_after_send":
            unknown.append(row.id)
    return {"count": count, "hash": digest.hexdigest(), "unknown_action_ids": unknown}


def _action_snapshot(action):
    return {field: getattr(action, field) for field in (
        "id", "status", "account_id", "action_type", "payload", "result",
        "scheduled_at", "executed_at", "lease_owner", "lease_expires_at",
    )}


def _apply_task(session, task, *, scope, before):
    config = {**(task.type_config or {}), "transport_contract_version": DIRECT_SEARCH_TRANSPORT_VERSION}
    if task.type == "search_rank_deboost":
        config["proxy_airport_node_id"] = None
    task.type_config = config
    audit(session, tenant_id=scope.tenant_id, actor=scope.actor, action="搜索当前授权直连合同迁移",
          target_type="task", target_id=task.id, detail=json.dumps({
              "reference": scope.reference, "before_config_hash": before["config_hash"],
              "after_config_hash": _hash(config), "protected_hash": before["protected_hash"],
              "old_version": before["old_version"], "new_version": DIRECT_SEARCH_TRANSPORT_VERSION,
              "existing_executions_changed": False,
          }, ensure_ascii=False, sort_keys=True))


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()

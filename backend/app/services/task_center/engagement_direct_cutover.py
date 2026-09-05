"""Audited fresh-task replacement; no old plan or remote result is replayed."""
from dataclasses import dataclass
import json

from sqlalchemy import select

from app.common.state_hash import canonical_state_hash
from app.models import AuditLog, Task
from app.services._common import _now, audit
from app.services.account_group_revision_bootstrap import preview_group_revisions
from app.services.account_group_revision_snapshot import lock_membership_tenant

from .engagement_replacement_config import (
    COMMON_FIELDS, authorization_snapshot, replacement_authorizations, replacement_payload,
    preserve_runtime_pacing, require_preserved_account_scope,
)
from .task_retirement import ENGAGEMENT_TYPES, RETIREMENT_DETAIL


CUTOVER_SCHEMA = 1
RETIRE_AUDIT = "统一引擎旧任务退役"
ACTIVATE_AUDIT = "统一引擎替代任务启动"


@dataclass(frozen=True)
class CutoverOperation:
    actor: str
    audit_reference: str
    deployed_sha: str


def preview_cutover(session, spec):
    tenant_id, changes = _validate_spec(spec)
    tasks = _exact_tasks(session, tenant_id, tuple(changes))
    _require_running_set(session, tenant_id, tasks)
    state = {"spec": spec, "tasks": [_task_preview(session, task, changes[task.id]) for task in tasks],
        "membership": preview_group_revisions(session, tenant_id)["state_hash"]}
    return {"schema_version": CUTOVER_SCHEMA, "state": state, "state_hash": canonical_state_hash(state)}


def _task_preview(session, task, overrides):
    payload = replacement_payload(task, overrides)
    require_preserved_account_scope(session, task, payload)
    before = {key: getattr(task, key) for key in (*COMMON_FIELDS, "id", "type", "status",
        "type_config", "task_lifecycle_epoch", "config_revision", "scheduled_start", "deleted_at",
        "retired_at", "replaced_by_task_id", "group_ai_prejoin_channel_ids")}
    return {"old": json.loads(json.dumps(before, default=str)), "replacement": payload.model_dump(mode="json"),
        "authorization": authorization_snapshot(session, task)}


def retire_cutover(session, preview, operation, *, create_replacement):
    _validate_preview(preview, operation)
    existing = _prior_receipt(session, preview["state_hash"], operation)
    if existing is not None:
        verify_retirement(session, existing)
        return existing
    spec = preview["state"]["spec"]
    tenant_id, changes = _validate_spec(spec)
    lock_membership_tenant(session, tenant_id)
    tasks = _exact_tasks(session, tenant_id, tuple(changes), lock=True)
    current = preview_cutover(session, spec)
    if current["state_hash"] != preview["state_hash"]:
        raise ValueError("engagement_cutover_preview_conflict")
    mapping, authorizations, replacement_hashes = {}, {}, {}
    for old in tasks:
        payload, cloned = replacement_authorizations(session, old, replacement_payload(old, changes[old.id]))
        new = create_replacement(session, old, payload)
        _require_fresh_replacement(old, new)
        preserve_runtime_pacing(old, new)
        mapping[old.id] = new.id
        authorizations.update(cloned)
        replacement_hashes[new.id] = replacement_config_hash(new)
    retired_at = _now()
    for old in tasks:
        old.retired_at = retired_at
        old.replaced_by_task_id = mapping[old.id]
        old.status = "stopped"
        old.next_run_at = None
        old.task_lifecycle_epoch = int(old.task_lifecycle_epoch or 1) + 1
        old.last_error = RETIREMENT_DETAIL
    receipt = {"schema_version": CUTOVER_SCHEMA, "tenant_id": tenant_id,
        "preview_hash": preview["state_hash"], "mapping": mapping, "authorization_mapping": authorizations,
        "replacement_hashes": replacement_hashes,
        "deployed_sha": operation.deployed_sha, "audit_reference": operation.audit_reference,
        "retired_at": retired_at.isoformat()}
    _audit_stage(session, receipt, operation, action=RETIRE_AUDIT)
    session.flush()
    return receipt


def verify_retirement(session, receipt):
    old = _exact_tasks(session, receipt["tenant_id"], tuple(receipt["mapping"]))
    new = _exact_tasks(session, receipt["tenant_id"], tuple(receipt["mapping"].values()))
    if any(not _retirement_matches(task, receipt) for task in old):
        raise ValueError("engagement_cutover_retirement_readback_mismatch")
    if any(not _replacement_matches(task, receipt) for task in new):
        raise ValueError("engagement_cutover_replacement_readback_mismatch")
    return old, new


def _retirement_matches(task, receipt):
    return (task.retired_at is not None and task.status == "stopped" and task.next_run_at is None
        and task.replaced_by_task_id == receipt["mapping"][task.id])


def _replacement_matches(task, receipt):
    return ((task.type_config or {}).get("engagement_contract_version") == "unified_engagement_v1"
        and task.retired_at is None
        and replacement_config_hash(task) == receipt["replacement_hashes"][task.id])


def replacement_config_hash(task):
    return canonical_state_hash({key: getattr(task, key) for key in (*COMMON_FIELDS, "type_config",
        "type", "config_revision", "scheduled_start", "group_ai_prejoin_channel_ids")})


def activate_cutover(session, receipt, operation, *, start_replacement, require_cleanup):
    _require_operation(operation)
    _require_receipt_audit(session, receipt)
    verify_retirement(session, receipt)
    new = _exact_tasks(session, receipt["tenant_id"], tuple(receipt["mapping"].values()), lock=True)
    if all(task.status in {"running", "pending"} for task in new):
        return {"activated": len(new), "already_active": True}
    if any(task.status != "draft" for task in new):
        raise ValueError("engagement_cutover_activation_state_conflict")
    require_cleanup(session, receipt)
    for task in new:
        start_replacement(session, task, operation.actor)
    _audit_stage(session, receipt, operation, action=ACTIVATE_AUDIT)
    session.flush()
    return {"activated": len(new), "already_active": False}


def _validate_spec(spec):
    if type(spec.get("tenant_id")) is not int or spec["tenant_id"] <= 0:
        raise ValueError("engagement_cutover_tenant_required")
    changes = spec.get("replacements")
    if not isinstance(changes, dict) or not changes or any(not isinstance(value, dict) for value in changes.values()):
        raise ValueError("engagement_cutover_exact_replacements_required")
    if not isinstance(spec.get("deployed_sha"), str) or len(spec["deployed_sha"]) != 40:
        raise ValueError("engagement_cutover_deployed_sha_required")
    return spec["tenant_id"], changes


def _validate_preview(preview, operation):
    _require_operation(operation)
    if (preview.get("schema_version") != CUTOVER_SCHEMA
            or canonical_state_hash(preview.get("state")) != preview.get("state_hash")):
        raise ValueError("engagement_cutover_preview_invalid")
    if preview["state"]["spec"]["deployed_sha"] != operation.deployed_sha:
        raise ValueError("engagement_cutover_deployed_sha_changed")


def _require_operation(operation):
    if not operation.actor or len(operation.actor) > 100:
        raise ValueError("engagement_cutover_actor_required")
    if not operation.audit_reference or len(operation.audit_reference) > 100:
        raise ValueError("engagement_cutover_audit_reference_required")
    if len(operation.deployed_sha) != 40:
        raise ValueError("engagement_cutover_deployed_sha_required")


def _exact_tasks(session, tenant_id, ids, *, lock=False):
    if len(set(ids)) != len(ids):
        raise ValueError("engagement_cutover_task_set_duplicate")
    query = select(Task).where(Task.tenant_id == tenant_id, Task.id.in_(ids)).order_by(Task.id)
    if lock:
        query = query.with_for_update(nowait=True).execution_options(populate_existing=True)
    rows = list(session.scalars(query))
    if {task.id for task in rows} != set(ids):
        raise ValueError("engagement_cutover_task_set_mismatch")
    return rows


def _require_running_set(session, tenant_id, tasks):
    if any(task.type not in ENGAGEMENT_TYPES or task.status != "running" or task.retired_at is not None
            or task.deleted_at is not None for task in tasks):
        raise ValueError("engagement_cutover_original_task_not_running")
    actual = set(session.scalars(select(Task.id).where(Task.tenant_id == tenant_id,
        Task.type.in_(ENGAGEMENT_TYPES), Task.status == "running", Task.deleted_at.is_(None))))
    if actual != {task.id for task in tasks}:
        raise ValueError("engagement_cutover_running_set_changed")


def _require_fresh_replacement(old, new):
    if (new.id == old.id or new.tenant_id != old.tenant_id or new.type != old.type
            or new.status != "draft" or new.config_revision != 1 or new.task_lifecycle_epoch != 1
            or (new.type_config or {}).get("engagement_contract_version") != "unified_engagement_v1"):
        raise ValueError("engagement_cutover_replacement_invalid")


def _prior_receipt(session, preview_hash, operation):
    row = session.scalar(select(AuditLog).where(AuditLog.action == RETIRE_AUDIT,
        AuditLog.target_type == "engagement_cutover", AuditLog.target_id == preview_hash))
    if row is None:
        return None
    receipt = json.loads(row.detail)
    if receipt["audit_reference"] != operation.audit_reference:
        raise ValueError("engagement_cutover_audit_reference_conflict")
    return receipt


def _require_receipt_audit(session, receipt):
    row = session.scalar(select(AuditLog).where(AuditLog.tenant_id == receipt["tenant_id"],
        AuditLog.action == RETIRE_AUDIT, AuditLog.target_type == "engagement_cutover",
        AuditLog.target_id == receipt["preview_hash"]))
    if row is None or json.loads(row.detail) != receipt:
        raise ValueError("engagement_cutover_receipt_invalid")


def _audit_stage(session, receipt, operation, *, action):
    audit(session, tenant_id=receipt["tenant_id"], actor=operation.actor, action=action,
        target_type="engagement_cutover", target_id=receipt["preview_hash"],
        detail=json.dumps(receipt, ensure_ascii=False, sort_keys=True))

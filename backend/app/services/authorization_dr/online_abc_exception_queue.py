from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from sqlalchemy import and_, func, or_, select

from app.models import (
    AuditLog,
    AuthorizationDrExecutionNode,
    AuthorizationDrRuntimeContract,
    TgAccount,
    TgAccountAuthorization,
    TgAuthorizationDrOperation,
    TgAuthorizationOnlineAbcBatch,
    TgAuthorizationOnlineAbcItem,
    TgAuthorizationOnlineAbcSlotResult,
)
from app.services._common import _now, audit

from . import online_abc as abc
from .abc_verify import preview_abc_e4
from .contracts import AuthorizationDrError
from .online_abc import UNKNOWN_OPERATION_STATUSES
from .online_abc_manifest import ACTIVE_OPERATION_STATUSES
from .online_abc_operations import online_abc_item_operations
from .online_abc_primary import primary_state
from .online_abc_exception_state import (
    audit_value as _audit_value,
    e4_remote_id as _e4_remote_id,
    item_snapshot as _item_snapshot,
    list_online_abc_exceptions,
    operation_snapshots as _operation_snapshots,
    primary_snapshot as _primary_snapshot,
    slot_snapshots as _slot_snapshots,
)


ACTION = "收集 ABC frozen-N 首轮异常"
CLASS_DEFERRED_ISSUE = "deferred_issue"
CLASS_DEFERRED_RECONCILE = "deferred_reconcile"
CLASS_COMPLETED = "completed_checkpoint"
KEY_PATTERN = re.compile(r"[A-Za-z0-9:._-]{1,100}")
SHA_PATTERN = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
REMOTE_UNCERTAIN_STATES = {"started", "unknown"}
TERMINAL_OPERATION_STATUSES = {
    "deferred_reconcile", "failed", "manual_required",
    "migration_rolled_back_forward", "succeeded",
}


@dataclass(frozen=True)
class ExceptionContext:
    batch: TgAuthorizationOnlineAbcBatch
    item: TgAuthorizationOnlineAbcItem
    slots: dict[str, TgAuthorizationOnlineAbcSlotResult]
    operations: dict[str, TgAuthorizationDrOperation | None]
    account: TgAccount
    primary: TgAccountAuthorization


def preview_online_abc_exception(
    session,
    batch_id: str,
    account_id: int,
    *,
    runtime_release_sha: str,
    idempotency_key: str,
    requested_by: str,
    approved_by: str,
    approval_ref: str,
) -> dict:
    context = _context(session, batch_id, account_id)
    approval = _approval(
        context.batch, requested_by=requested_by,
        approved_by=approved_by, approval_ref=approval_ref,
    )
    release_sha = _release_sha(runtime_release_sha)
    _require_execution_release(context.batch, release_sha)
    key = _key(idempotency_key)
    classification, operation = _classify(session, context, key)
    global_state = _global_boundary(session, operation)
    payload = _payload(
        session, context, classification=classification, operation=operation,
        global_state=global_state, release_sha=release_sha, key=key, approval=approval,
    )
    return {**payload, "fingerprint": _fingerprint(payload)}


def apply_online_abc_exception(
    session,
    batch_id: str,
    account_id: int,
    *,
    runtime_release_sha: str,
    idempotency_key: str,
    expected_fingerprint: str,
    requested_by: str,
    approved_by: str,
    approval_ref: str,
) -> dict:
    existing = _existing(session, batch_id, account_id, key=idempotency_key)
    if existing:
        return _idempotent(existing, expected_fingerprint)
    _lock_context(session, batch_id, account_id)
    preview = preview_online_abc_exception(
        session, batch_id, account_id, runtime_release_sha=runtime_release_sha,
        idempotency_key=idempotency_key, requested_by=requested_by,
        approved_by=approved_by, approval_ref=approval_ref,
    )
    if preview["fingerprint"] != expected_fingerprint:
        raise AuthorizationDrError("migration_fingerprint_conflict", "Exception preview changed")
    context = apply_previewed_online_abc_exception(session, preview)
    session.commit()
    return {
        **_result(session, context.batch.id, context.item.account_id, preview=preview),
        "already_applied": False,
    }


def readback_online_abc_exception(
    session, batch_id: str, account_id: int, *, idempotency_key: str,
) -> dict:
    existing = _existing(session, batch_id, account_id, key=idempotency_key)
    if not existing:
        raise AuthorizationDrError("online_abc_exception_not_found", "Exception audit is unavailable")
    return {**existing, "already_applied": True}


def apply_previewed_online_abc_exception(session, preview: dict) -> ExceptionContext:
    _lock_context(session, preview["batch_id"], preview["account_id"])
    context = _context(session, preview["batch_id"], preview["account_id"])
    _apply_classification(session, context, preview)
    return context


def _classify(session, context: ExceptionContext, key: str):
    if _completed_checkpoint(session, context, key):
        return CLASS_COMPLETED, None
    operation = _uncertain_operation(context.operations)
    if operation:
        _require_quarantinable(operation)
        _require_primary_frozen(context)
        return CLASS_DEFERRED_RECONCILE, operation
    if _has_open_operation(context.operations):
        raise AuthorizationDrError("online_abc_exception_operation_active", "Remote operation is still active")
    if not context.item.blocker_code and context.item.status == "running":
        raise AuthorizationDrError("online_abc_exception_unclassified", "Running item has no terminal evidence")
    return CLASS_DEFERRED_ISSUE, _terminal_problem_operation(context.operations)


def _completed_checkpoint(session, context: ExceptionContext, key: str) -> bool:
    operations = context.operations
    if not all(operations[name] and operations[name].status == "succeeded" for name in ("b", "c", "e4")):
        return False
    try:
        preview_abc_e4(
            session, context.batch.tenant_id, context.item.account_id,
            idempotency_key=f"{key}:artifact-readback",
        )
    except AuthorizationDrError:
        return False
    return bool(_e4_remote_id(session, operations["e4"].id) and _primary_frozen(context))


def _uncertain_operation(operations: dict):
    rows = [operation for operation in operations.values() if operation and _is_uncertain(operation)]
    if len(rows) > 1:
        raise AuthorizationDrError("online_abc_exception_operation_ambiguous", "Multiple remote operations are uncertain")
    return rows[0] if rows else None


def _is_uncertain(operation) -> bool:
    return bool(
        operation.status in UNKNOWN_OPERATION_STATUSES
        or operation.status in ACTIVE_OPERATION_STATUSES
        or (
            operation.remote_call_state in REMOTE_UNCERTAIN_STATES
            and operation.status not in TERMINAL_OPERATION_STATUSES
        )
    )


def _require_quarantinable(operation) -> None:
    live_lease = bool(operation.lease_expires_at and operation.lease_expires_at > _now())
    if operation.owner_node_id or live_lease:
        raise AuthorizationDrError("online_abc_exception_owner_active", "Operation owner or lease is active")


def _global_boundary(session, operation) -> dict:
    runtime = session.get(AuthorizationDrRuntimeContract, 1)
    if not runtime or runtime.mode != "off" or runtime.claim_scope_operation_id:
        raise AuthorizationDrError("runtime_capability_unproven", "DR runtime is not safely off")
    sensitive = list(session.scalars(select(TgAuthorizationDrOperation.id).where(or_(
        TgAuthorizationDrOperation.status.in_(ACTIVE_OPERATION_STATUSES),
        and_(
            TgAuthorizationDrOperation.remote_call_state.in_(REMOTE_UNCERTAIN_STATES),
            TgAuthorizationDrOperation.status.not_in(TERMINAL_OPERATION_STATUSES),
        ),
    ))))
    allowed = {operation.id} if operation else set()
    if set(sensitive) - allowed or len(sensitive) != len(set(sensitive)):
        raise AuthorizationDrError("online_abc_exception_global_active", "Another sensitive operation exists")
    my_clients = int(session.scalar(select(func.coalesce(func.sum(
        AuthorizationDrExecutionNode.active_client_count,
    ), 0)).where(AuthorizationDrExecutionNode.region_code == "my")) or 0)
    if my_clients:
        raise AuthorizationDrError("malaysia_client_leak", "Malaysia active client count must be zero")
    return {"runtime_mode": "off", "runtime_scope": "", "sensitive_ids": sensitive, "my_clients": 0}


def _payload(
    session, context: ExceptionContext, *, classification: str, operation,
    global_state: dict, release_sha: str, key: str, approval: tuple[str, str, str],
) -> dict:
    return {
        "batch_id": context.batch.id, "batch_version": context.batch.version,
        "batch_status": context.batch.status, "selection_mode": context.batch.selection_mode,
        "account_id": context.item.account_id, "item": _item_snapshot(context.item),
        "slots": _slot_snapshots(context.slots), "operations": _operation_snapshots(session, context.operations),
        "primary": _primary_snapshot(context), "classification": classification,
        "operation_id": operation.id if operation else "",
        "original_blocker_code": _blocker(context, operation),
        "global": global_state, "previous_execution_release_sha": context.batch.execution_release_sha,
        "runtime_release_sha": release_sha, "idempotency_key": key,
        "requested_by": approval[0], "approved_by": approval[1], "approval_ref": approval[2],
    }


def _apply_classification(session, context: ExceptionContext, preview: dict) -> None:
    classification = preview["classification"]
    if classification == CLASS_COMPLETED:
        _complete_checkpoint(session, context, preview)
    elif classification == CLASS_DEFERRED_RECONCILE:
        _quarantine(session, context, preview)
    else:
        _defer_issue(session, context, preview)
    _advance_batch(session, context.batch)
    _audit_exception(session, context, preview)


def _complete_checkpoint(session, context: ExceptionContext, preview: dict) -> None:
    abc._sync_primary_probe(session, context.item)
    abc._sync_slot(session, context.item, "standby_1", context.operations["b"])
    abc._sync_slot(session, context.item, "standby_2", context.operations["c"])
    abc._complete_item(
        session, context.batch, context.item, preview["approved_by"], preview["approval_ref"],
    )
    if context.item.outcome != "succeeded":
        raise AuthorizationDrError("online_abc_completed_checkpoint_projection_failed", "Checkpoint did not close")


def _quarantine(session, context: ExceptionContext, preview: dict) -> None:
    operation = session.get(TgAuthorizationDrOperation, preview["operation_id"])
    operation.status = "deferred_reconcile"
    operation.remote_call_state = "unknown"
    operation.reconcile_status = "quarantined"
    operation.operation_version += 1
    operation.finished_at = _now()
    _project_exception_slots(context, operation, unresolved=True)
    _finish_exception_item(context.item, "deferred_reconcile", preview["original_blocker_code"])


def _defer_issue(session, context: ExceptionContext, preview: dict) -> None:
    operation_id = preview["operation_id"]
    operation = session.get(TgAuthorizationDrOperation, operation_id) if operation_id else None
    _project_exception_slots(context, operation, unresolved=False)
    _finish_exception_item(context.item, "manual_required", preview["original_blocker_code"])


def _project_exception_slots(context: ExceptionContext, operation, *, unresolved: bool) -> None:
    for name in ("standby_1", "standby_2"):
        slot = context.slots[name]
        mapped = context.operations["b" if name == "standby_1" else "c"]
        if mapped and mapped.status == "succeeded":
            slot.outcome = "succeeded"
            slot.operation_id = mapped.id
            slot.blocker_code = ""
        elif operation and mapped and mapped.id == operation.id:
            slot.outcome = "deferred_reconcile" if unresolved else "manual_required"
            slot.operation_id = mapped.id
            slot.blocker_code = operation.blocker_code
        elif slot.outcome not in {"already_qualified", "succeeded"}:
            slot.outcome = "deferred_issue"
            slot.blocker_code = "upstream_exception"
        slot.version += 1


def _finish_exception_item(item, outcome: str, blocker: str) -> None:
    item.status = outcome
    item.outcome = outcome
    item.blocker_code = (blocker or outcome)[:100]
    item.finished_at = _now()
    item.version += 1


def _advance_batch(session, batch) -> None:
    pending = session.scalar(select(func.count()).select_from(TgAuthorizationOnlineAbcItem).where(
        TgAuthorizationOnlineAbcItem.batch_id == batch.id,
        TgAuthorizationOnlineAbcItem.status == "pending",
    ))
    if pending:
        batch.status = "sweeping"
    else:
        exceptions = session.scalar(select(func.count()).select_from(TgAuthorizationOnlineAbcItem).where(
            TgAuthorizationOnlineAbcItem.batch_id == batch.id,
            TgAuthorizationOnlineAbcItem.outcome.in_({"manual_required", "deferred_reconcile"}),
        ))
        batch.status = "completed_with_exceptions" if exceptions else "completed"
    batch.version += 1


def _audit_exception(session, context: ExceptionContext, preview: dict) -> None:
    audit(
        session, tenant_id=context.batch.tenant_id, actor=preview["approved_by"], action=ACTION,
        target_type="tg_authorization_online_abc_items", target_id=context.item.id,
        detail=(
            f"account_id={context.item.account_id}; classification={preview['classification']}; "
            f"blocker={preview['original_blocker_code']}; operation={preview['operation_id']}; "
            f"idempotency_key={preview['idempotency_key']}; fingerprint={preview['fingerprint']}; "
            f"approval_ref={preview['approval_ref']}; primary_id={preview['primary']['authorization_id']}; "
            f"current_primary_id={preview['primary']['current_authorization_id']}; "
            f"account_status={preview['primary']['account_status']}; "
            f"account_session_digest={preview['primary']['account_session_digest']}; "
            f"primary_session_digest={preview['primary']['session_digest']}; "
            f"primary_app_id={preview['primary']['primary_app_id']}; "
            f"primary_uid_digest={preview['primary']['telegram_user_id_digest']}; "
            f"primary_auth_key_digest={preview['primary']['auth_key_fingerprint_digest']}; "
            f"primary_is_current={str(preview['primary']['primary_is_current']).lower()}; "
            f"primary_is_slot_current={str(preview['primary']['primary_is_slot_current']).lower()}; "
            f"primary_protected_from_cleanup={str(preview['primary']['primary_protected_from_cleanup']).lower()}; "
            f"primary_status={preview['primary']['primary_status']}; "
            f"primary_health_status={preview['primary']['primary_health_status']}; "
            f"primary_generations={json.dumps(preview['primary']['generations'], separators=(',', ':'))}"
        ),
    )


def _context(session, batch_id: str, account_id: int) -> ExceptionContext:
    batch = _batch(session, batch_id)
    item = session.scalar(select(TgAuthorizationOnlineAbcItem).where(
        TgAuthorizationOnlineAbcItem.batch_id == batch.id,
        TgAuthorizationOnlineAbcItem.account_id == account_id,
    ))
    if not item or item.status not in {"running", "stopped"}:
        raise AuthorizationDrError("online_abc_exception_item_invalid", "Current exception item is unavailable")
    current = list(session.scalars(select(TgAuthorizationOnlineAbcItem.id).where(
        TgAuthorizationOnlineAbcItem.batch_id == batch.id,
        TgAuthorizationOnlineAbcItem.status.in_({"running", "stopped"}),
    )))
    if current != [item.id]:
        raise AuthorizationDrError("online_abc_exception_item_ambiguous", "Exactly one current item is required")
    slots = _slots(session, item.id)
    account = session.get(TgAccount, item.account_id)
    primary = session.get(TgAccountAuthorization, item.primary_authorization_id)
    if not account or not primary or set(slots) != {"standby_1", "standby_2"}:
        raise AuthorizationDrError("online_abc_exception_item_invalid", "Frozen item facts are incomplete")
    return ExceptionContext(batch, item, slots, online_abc_item_operations(session, batch, item), account, primary)


def _lock_context(session, batch_id: str, account_id: int) -> None:
    batch = session.scalar(select(TgAuthorizationOnlineAbcBatch).where(
        TgAuthorizationOnlineAbcBatch.id == batch_id,
    ).with_for_update().execution_options(populate_existing=True))
    item = session.scalar(select(TgAuthorizationOnlineAbcItem).where(
        TgAuthorizationOnlineAbcItem.batch_id == batch_id,
        TgAuthorizationOnlineAbcItem.account_id == account_id,
    ).with_for_update().execution_options(populate_existing=True))
    if not batch or not item:
        raise AuthorizationDrError("online_abc_item_not_found", "ABC item is unavailable")
    list(session.scalars(select(TgAuthorizationOnlineAbcSlotResult).where(
        TgAuthorizationOnlineAbcSlotResult.item_id == item.id,
    ).with_for_update().execution_options(populate_existing=True)))


def _batch(session, batch_id: str):
    batch = session.get(TgAuthorizationOnlineAbcBatch, batch_id)
    if not batch or batch.selection_mode != "all_online_accounts":
        raise AuthorizationDrError("online_abc_batch_not_found", "Full frozen-N batch is unavailable")
    return batch


def _slots(session, item_id: str) -> dict:
    rows = session.scalars(select(TgAuthorizationOnlineAbcSlotResult).where(
        TgAuthorizationOnlineAbcSlotResult.item_id == item_id,
    ))
    return {row.logical_slot: row for row in rows}


def _primary_frozen(context: ExceptionContext) -> bool:
    return primary_state(context.account, context.primary, context.item) in {
        "frozen", "legacy_frozen", "qualified",
    }


def _require_primary_frozen(context: ExceptionContext) -> None:
    if not _primary_frozen(context):
        raise AuthorizationDrError("online_abc_primary_drift", "A changed before quarantine")


def _require_execution_release(batch, release_sha: str) -> None:
    expected = batch.execution_release_sha or batch.deployed_release_sha
    if expected != release_sha:
        raise AuthorizationDrError(
            "online_abc_sweep_release_rebind_required",
            "Batch execution release must be rebound before exception closure",
        )


def _has_open_operation(operations: dict) -> bool:
    return any(operation and _is_uncertain(operation) for operation in operations.values())


def _terminal_problem_operation(operations: dict):
    rows = [operation for operation in operations.values() if operation and operation.status != "succeeded"]
    return rows[-1] if rows else None


def _blocker(context: ExceptionContext, operation) -> str:
    return (operation.blocker_code if operation and operation.blocker_code else context.item.blocker_code) or "unclassified_issue"


def _approval(
    batch, *, requested_by: str, approved_by: str, approval_ref: str,
) -> tuple[str, str, str]:
    values = tuple(value.strip() for value in (requested_by, approved_by, approval_ref))
    if not all(values) or values[0] == values[1] or values != (
        batch.requested_by, batch.approved_by, batch.approval_ref,
    ):
        raise AuthorizationDrError("online_abc_runner_approval_mismatch", "Sweep approval differs from batch")
    return values


def _release_sha(value: str) -> str:
    normalized = value.strip().lower()
    if not SHA_PATTERN.fullmatch(normalized):
        raise AuthorizationDrError("runtime_image_mismatch", "Exact current release SHA is required")
    return normalized


def _key(value: str) -> str:
    normalized = value.strip()
    if not KEY_PATTERN.fullmatch(normalized):
        raise AuthorizationDrError("idempotency_key_required", "Exception idempotency key is invalid")
    return normalized


def _fingerprint(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _existing(session, batch_id: str, account_id: int, *, key: str) -> dict | None:
    normalized = _key(key)
    item = session.scalar(select(TgAuthorizationOnlineAbcItem).where(
        TgAuthorizationOnlineAbcItem.batch_id == batch_id,
        TgAuthorizationOnlineAbcItem.account_id == account_id,
    ))
    if not item:
        return None
    row = session.scalar(select(AuditLog).where(
        AuditLog.target_type == "tg_authorization_online_abc_items",
        AuditLog.target_id == item.id, AuditLog.action == ACTION,
        AuditLog.detail.contains(f"idempotency_key={normalized};"),
    ).order_by(AuditLog.id.desc()).limit(1))
    if not row:
        return None
    fingerprint = _audit_value(row.detail, "fingerprint")
    classification = _audit_value(row.detail, "classification")
    return _result(
        session, batch_id, account_id,
        preview={"fingerprint": fingerprint, "classification": classification},
    )


def _idempotent(existing: dict, expected_fingerprint: str) -> dict:
    if existing["fingerprint"] != expected_fingerprint:
        raise AuthorizationDrError("idempotency_key_conflict", "Exception key was already used")
    return {**existing, "already_applied": True}


def _result(session, batch_id: str, account_id: int, *, preview: dict) -> dict:
    batch = _batch(session, batch_id)
    item = session.scalar(select(TgAuthorizationOnlineAbcItem).where(
        TgAuthorizationOnlineAbcItem.batch_id == batch.id,
        TgAuthorizationOnlineAbcItem.account_id == account_id,
    ))
    return {
        "batch_id": batch.id, "batch_status": batch.status, "batch_version": batch.version,
        "account_id": item.account_id, "item_status": item.status, "item_outcome": item.outcome,
        "item_version": item.version, "classification": preview["classification"],
        "fingerprint": preview["fingerprint"],
    }

__all__ = [
    "apply_online_abc_exception", "apply_previewed_online_abc_exception",
    "list_online_abc_exceptions",
    "preview_online_abc_exception", "readback_online_abc_exception",
]

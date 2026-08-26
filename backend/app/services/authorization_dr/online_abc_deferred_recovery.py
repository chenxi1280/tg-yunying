from __future__ import annotations

import hashlib
import json
import re
from collections import Counter

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
from .online_abc import UNKNOWN_OPERATION_STATUSES, online_abc_batch_status
from .online_abc_exception_state import (
    e4_remote_id,
    require_exception_primaries_unchanged,
)
from .online_abc_deferred_manifest import canonical_deferred_manifest
from .online_abc_manifest import ACTIVE_OPERATION_STATUSES
from .online_abc_operations import online_abc_item_operations
from .online_abc_primary import primary_state


START_ACTION = "启动 ABC deferred recovery sweep"
ITEM_ACTION = "ABC deferred recovery item"
FINAL_ACTION = "ABC deferred recovery final"
PAUSE_ACTION = "暂停 ABC deferred recovery sweep"
KEY_PATTERN = re.compile(r"[A-Za-z0-9:._-]{1,100}")
SHA_PATTERN = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
TERMINAL_ITEM_STATUSES = {"deferred_reconcile", "manual_required", "succeeded"}
TERMINAL_OPERATION_STATUSES = {
    "deferred_reconcile", "failed", "manual_required",
    "migration_rolled_back_forward", "succeeded",
}


def preview_deferred_recovery_start(
    session,
    batch_id: str,
    *,
    runtime_release_sha: str,
    idempotency_key: str,
    expected_deferred_count: int,
    requested_by: str,
    approved_by: str,
    approval_ref: str,
) -> dict:
    batch = _batch(session, batch_id)
    _require_startable(batch)
    _require_approval(batch, requested_by, approved_by, approval_ref)
    release_sha = _release_sha(runtime_release_sha)
    if (batch.execution_release_sha or batch.deployed_release_sha) != release_sha:
        raise AuthorizationDrError("runtime_image_mismatch", "Deferred recovery must run on batch execution SHA")
    manifest = canonical_deferred_manifest(session, batch.id, runtime_release_sha=release_sha)
    if manifest["row_count"] != expected_deferred_count or expected_deferred_count <= 0:
        raise AuthorizationDrError("deferred_recovery_target_count_mismatch", "Deferred target count changed")
    _require_no_active_recovery(session, batch.id)
    _require_global_quiescent(session)
    payload = {
        "batch_id": batch.id, "batch_version": batch.version, "batch_status": batch.status,
        "target_count": batch.target_count, "deferred_count": manifest["row_count"],
        "manifest_hash": manifest["manifest_hash"], "runtime_release_sha": release_sha,
        "idempotency_key": _key(idempotency_key), "requested_by": requested_by.strip(),
        "approved_by": approved_by.strip(), "approval_ref": approval_ref.strip(),
        "manual_required_untouched": True, "until_exhausted": True,
    }
    return {**payload, "fingerprint": _fingerprint(payload), "groups": manifest["groups"]}


def apply_deferred_recovery_start(
    session,
    batch_id: str,
    *,
    runtime_release_sha: str,
    idempotency_key: str,
    expected_deferred_count: int,
    expected_fingerprint: str,
    requested_by: str,
    approved_by: str,
    approval_ref: str,
) -> dict:
    existing = _existing_start(session, batch_id, idempotency_key)
    if existing:
        return _idempotent(existing, expected_fingerprint)
    _lock_batch(session, batch_id)
    preview = preview_deferred_recovery_start(
        session, batch_id, runtime_release_sha=runtime_release_sha,
        idempotency_key=idempotency_key, expected_deferred_count=expected_deferred_count,
        requested_by=requested_by, approved_by=approved_by, approval_ref=approval_ref,
    )
    if preview["fingerprint"] != expected_fingerprint:
        raise AuthorizationDrError("migration_fingerprint_conflict", "Deferred recovery preview changed")
    batch = _lock_batch(session, batch_id)
    batch.status = "deferred_recovery"
    batch.version += 1
    _audit_start(session, batch, preview)
    session.commit()
    return {**deferred_recovery_status(session, batch.id), "already_applied": False}


def readback_deferred_recovery_start(session, batch_id: str, *, idempotency_key: str) -> dict:
    existing = _existing_start(session, batch_id, idempotency_key)
    if not existing:
        raise AuthorizationDrError("deferred_recovery_start_not_found", "Deferred recovery start audit is missing")
    return {**existing, "already_applied": True}


def run_deferred_recovery_once(session, *, runtime_release_sha: str) -> dict:
    batch, key = _active_recovery(session)
    if not batch:
        return {"status": "idle"}
    _release_sha(runtime_release_sha)
    _require_global_quiescent(session)
    require_exception_primaries_unchanged(session, batch.id)
    item = _next_unprocessed_item(session, batch.id, key)
    if not item:
        return _finish(session, batch, key)
    result = _process_item(session, batch, item, key)
    session.commit()
    return {**deferred_recovery_status(session, batch.id), "last_item": result}


def pause_deferred_recovery_for_error(session, blocker: str) -> dict:
    batch, _key_value = _active_recovery(session)
    if not batch:
        return {"status": "idle"}
    batch = _lock_batch(session, batch.id)
    batch.status = "deferred_recovery_paused"
    batch.version += 1
    audit(
        session, tenant_id=batch.tenant_id, actor=batch.approved_by,
        action=PAUSE_ACTION, target_type="tg_authorization_online_abc_batches",
        target_id=batch.id,
        detail=f"approval_ref={batch.approval_ref}; blocker={blocker[:100]}; no_further_rpc=true",
    )
    session.commit()
    return deferred_recovery_status(session, batch.id)


def deferred_recovery_status(session, batch_id: str) -> dict:
    batch_status = online_abc_batch_status(session, batch_id)
    start = _latest_start(session, batch_id)
    key = _audit_value(start.detail, "idempotency_key") if start else ""
    processed = _processed_count(session, batch_id, key) if key else 0
    counts = Counter(batch_status["account_outcome_counts"])
    return {
        "batch": batch_status,
        "deferred_recovery": {
            "active": batch_status["status"] == "deferred_recovery",
            "paused": batch_status["status"] == "deferred_recovery_paused",
            "processed_count": processed,
            "remaining_deferred_to_rejudge": _remaining_count(session, batch_id, key) if key else 0,
            "deferred_count": counts["deferred_reconcile"],
            "manual_required_count": counts["manual_required"],
            "succeeded_count": counts["succeeded"],
            "terminal_count": sum(counts[name] for name in TERMINAL_ITEM_STATUSES),
            "idempotency_key": key,
        },
    }


def _process_item(session, batch, item, key: str) -> dict:
    before = item.outcome
    classification = _classification(session, batch, item)
    if classification["result"] == "succeeded":
        _complete_checkpoint(session, batch, item)
    elif classification["result"] == "manual_required":
        _mark_manual(session, item, classification["blocker"])
    _keep_recovery_active(batch)
    _audit_item(session, batch, item, key, before, classification)
    return {"account_id": item.account_id, **classification}


def _classification(session, batch, item) -> dict:
    operations = online_abc_item_operations(session, batch, item)
    if _completed_checkpoint(session, batch, item, operations):
        return {"result": "succeeded", "blocker": "", "reason": "completed_checkpoint_forward"}
    operation = _problem_operation(operations)
    if operation and _operation_manual(operation):
        return {"result": "manual_required", "blocker": operation.blocker_code, "reason": "terminal_failure"}
    if primary_state(
        session.get(TgAccount, item.account_id),
        session.get(TgAccountAuthorization, item.primary_authorization_id),
        item,
    ) not in {"frozen", "legacy_frozen", "qualified"}:
        return {"result": "deferred_reconcile", "blocker": "online_abc_primary_drift", "reason": "primary_drift"}
    blocker = operation.blocker_code if operation else item.blocker_code
    return {"result": "deferred_reconcile", "blocker": blocker, "reason": "same_operation_remote_unknown"}


def _completed_checkpoint(session, batch, item, operations: dict) -> bool:
    if not all(operations[name] and operations[name].status == "succeeded" for name in ("b", "c", "e4")):
        return False
    if not e4_remote_id(session, operations["e4"].id):
        return False
    try:
        preview_abc_e4(session, batch.tenant_id, item.account_id, idempotency_key=f"deferred-read:{item.id}")
    except AuthorizationDrError:
        return False
    return True


def _complete_checkpoint(session, batch, item) -> None:
    operations = online_abc_item_operations(session, batch, item)
    abc._sync_primary_probe(session, item)
    abc._sync_slot(session, item, "standby_1", operations["b"])
    abc._sync_slot(session, item, "standby_2", operations["c"])
    abc._complete_item(session, batch, item, batch.approved_by, batch.approval_ref)
    if item.outcome != "succeeded":
        raise AuthorizationDrError("deferred_recovery_completion_failed", "Completed checkpoint did not project")


def _mark_manual(session, item, blocker: str) -> None:
    item.status = "manual_required"
    item.outcome = "manual_required"
    item.blocker_code = (blocker or "manual_required")[:100]
    item.finished_at = _now()
    item.version += 1
    for slot in session.scalars(select(TgAuthorizationOnlineAbcSlotResult).where(
        TgAuthorizationOnlineAbcSlotResult.item_id == item.id,
    )):
        if slot.outcome not in {"already_qualified", "succeeded"}:
            slot.outcome = "manual_required"
            slot.blocker_code = item.blocker_code
            slot.version += 1


def _keep_recovery_active(batch) -> None:
    if batch.status != "deferred_recovery":
        batch.status = "deferred_recovery"
        batch.version += 1


def _finish(session, batch, key: str) -> dict:
    batch = _lock_batch(session, batch.id)
    counts = Counter(online_abc_batch_status(session, batch.id)["account_outcome_counts"])
    batch.status = "completed_with_exceptions" if counts["manual_required"] or counts["deferred_reconcile"] else "completed"
    batch.version += 1
    audit(
        session, tenant_id=batch.tenant_id, actor=batch.approved_by,
        action=FINAL_ACTION, target_type="tg_authorization_online_abc_batches", target_id=batch.id,
        detail=(
            f"idempotency_key={key}; processed_count={_processed_count(session, batch.id, key)}; "
            f"succeeded={counts['succeeded']}; manual_required={counts['manual_required']}; "
            f"deferred_reconcile={counts['deferred_reconcile']}; terminal_total="
            f"{sum(counts[name] for name in TERMINAL_ITEM_STATUSES)};"
        ),
    )
    session.commit()
    return deferred_recovery_status(session, batch.id)


def _problem_operation(operations: dict):
    rows = [operation for operation in operations.values() if operation and operation.status != "succeeded"]
    return rows[-1] if rows else None


def _operation_manual(operation) -> bool:
    return operation.status in {"failed", "manual_required", "migration_rolled_back_forward"}


def _require_global_quiescent(session) -> None:
    runtime = session.get(AuthorizationDrRuntimeContract, 1)
    if not runtime or runtime.mode != "off" or runtime.claim_scope_operation_id:
        raise AuthorizationDrError("runtime_capability_unproven", "DR runtime is not safely off")
    sensitive = session.scalar(select(TgAuthorizationDrOperation.id).where(or_(
        TgAuthorizationDrOperation.status.in_(ACTIVE_OPERATION_STATUSES),
        TgAuthorizationDrOperation.status.in_(UNKNOWN_OPERATION_STATUSES),
        and_(
            TgAuthorizationDrOperation.remote_call_state.in_({"started", "unknown"}),
            TgAuthorizationDrOperation.status.not_in(TERMINAL_OPERATION_STATUSES),
        ),
    )).limit(1))
    my_clients = int(session.scalar(select(func.coalesce(func.sum(
        AuthorizationDrExecutionNode.active_client_count,
    ), 0)).where(AuthorizationDrExecutionNode.region_code == "my")) or 0)
    if sensitive or my_clients:
        raise AuthorizationDrError("deferred_recovery_not_quiescent", "Global DR boundary is active")


def _require_startable(batch) -> None:
    if batch.selection_mode != "all_online_accounts" or batch.status != "completed_with_exceptions":
        raise AuthorizationDrError("deferred_recovery_start_invalid", f"Batch is {batch.status}")


def _require_approval(batch, requested_by: str, approved_by: str, approval_ref: str) -> None:
    values = tuple(value.strip() for value in (requested_by, approved_by, approval_ref))
    if not all(values) or values[0] == values[1] or values != (
        batch.requested_by, batch.approved_by, batch.approval_ref,
    ):
        raise AuthorizationDrError("online_abc_runner_approval_mismatch", "Recovery approval differs from batch")


def _active_recovery(session):
    start = session.scalar(select(AuditLog).where(
        AuditLog.action == START_ACTION,
        AuditLog.target_type == "tg_authorization_online_abc_batches",
    ).order_by(AuditLog.id.desc()).limit(1))
    if not start:
        return None, ""
    key = _audit_value(start.detail or "", "idempotency_key")
    finished = _final_audit(session, str(start.target_id), key)
    batch = session.get(TgAuthorizationOnlineAbcBatch, start.target_id)
    if finished or not batch or batch.status != "deferred_recovery":
        return None, ""
    return batch, key


def _require_no_active_recovery(session, batch_id: str) -> None:
    batch, _key_value = _active_recovery(session)
    if batch and batch.id != batch_id:
        raise AuthorizationDrError("deferred_recovery_already_active", "Another deferred recovery is active")


def _next_unprocessed_item(session, batch_id: str, key: str):
    for item in session.scalars(select(TgAuthorizationOnlineAbcItem).where(
        TgAuthorizationOnlineAbcItem.batch_id == batch_id,
        TgAuthorizationOnlineAbcItem.outcome == "deferred_reconcile",
    ).order_by(TgAuthorizationOnlineAbcItem.ordinal)):
        if not _item_audit(session, item.id, key):
            return item
    return None


def _remaining_count(session, batch_id: str, key: str) -> int:
    return sum(1 for _ in _iter_unprocessed(session, batch_id, key))


def _iter_unprocessed(session, batch_id: str, key: str):
    for item in session.scalars(select(TgAuthorizationOnlineAbcItem).where(
        TgAuthorizationOnlineAbcItem.batch_id == batch_id,
        TgAuthorizationOnlineAbcItem.outcome == "deferred_reconcile",
    )):
        if not _item_audit(session, item.id, key):
            yield item


def _processed_count(session, batch_id: str, key: str) -> int:
    rows = session.scalars(select(AuditLog).where(
        AuditLog.action == ITEM_ACTION,
        AuditLog.target_type == "tg_authorization_online_abc_items",
        AuditLog.detail.contains(f"idempotency_key={key};"),
    ))
    return sum(1 for row in rows if _audit_value(row.detail, "batch_id") == batch_id)


def _item_audit(session, item_id: str, key: str):
    return session.scalar(select(AuditLog).where(
        AuditLog.action == ITEM_ACTION,
        AuditLog.target_type == "tg_authorization_online_abc_items",
        AuditLog.target_id == item_id,
        AuditLog.detail.contains(f"idempotency_key={key};"),
    ).limit(1))


def _audit_start(session, batch, preview: dict) -> None:
    audit(
        session, tenant_id=batch.tenant_id, actor=preview["approved_by"],
        action=START_ACTION, target_type="tg_authorization_online_abc_batches", target_id=batch.id,
        detail=(
            f"idempotency_key={preview['idempotency_key']}; fingerprint={preview['fingerprint']}; "
            f"manifest_hash={preview['manifest_hash']}; deferred_count={preview['deferred_count']}; "
            f"approval_ref={preview['approval_ref']}; runtime_release={preview['runtime_release_sha']}; "
            "manual_required_untouched=true; until_exhausted=true;"
        ),
    )


def _audit_item(session, batch, item, key: str, before: str, classification: dict) -> None:
    audit(
        session, tenant_id=batch.tenant_id, actor=batch.approved_by,
        action=ITEM_ACTION, target_type="tg_authorization_online_abc_items", target_id=item.id,
        detail=(
            f"batch_id={batch.id}; idempotency_key={key}; before={before}; after={item.outcome}; "
            f"result={classification['result']}; reason={classification['reason']}; "
            f"blocker={classification['blocker']}; approval_ref={batch.approval_ref}"
        ),
    )


def _existing_start(session, batch_id: str, key: str) -> dict | None:
    normalized = _key(key)
    row = session.scalar(select(AuditLog).where(
        AuditLog.action == START_ACTION,
        AuditLog.target_type == "tg_authorization_online_abc_batches",
        AuditLog.target_id == batch_id,
        AuditLog.detail.contains(f"idempotency_key={normalized};"),
    ).order_by(AuditLog.id.desc()).limit(1))
    if not row:
        return None
    fingerprint = _audit_value(row.detail or "", "fingerprint")
    return {**deferred_recovery_status(session, batch_id), "fingerprint": fingerprint}


def _latest_start(session, batch_id: str):
    return session.scalar(select(AuditLog).where(
        AuditLog.action == START_ACTION,
        AuditLog.target_type == "tg_authorization_online_abc_batches",
        AuditLog.target_id == batch_id,
    ).order_by(AuditLog.id.desc()).limit(1))


def _final_audit(session, batch_id: str, key: str):
    return session.scalar(select(AuditLog).where(
        AuditLog.action == FINAL_ACTION,
        AuditLog.target_type == "tg_authorization_online_abc_batches",
        AuditLog.target_id == batch_id,
        AuditLog.detail.contains(f"idempotency_key={key};"),
    ).order_by(AuditLog.id.desc()).limit(1))


def _idempotent(existing: dict, expected_fingerprint: str) -> dict:
    if existing["fingerprint"] != expected_fingerprint:
        raise AuthorizationDrError("idempotency_key_conflict", "Deferred recovery key was already used")
    return {**existing, "already_applied": True}


def _batch(session, batch_id: str):
    batch = session.get(TgAuthorizationOnlineAbcBatch, batch_id)
    if not batch:
        raise AuthorizationDrError("online_abc_batch_not_found", "Online ABC batch is unavailable")
    return batch


def _lock_batch(session, batch_id: str):
    batch = session.scalar(select(TgAuthorizationOnlineAbcBatch).where(
        TgAuthorizationOnlineAbcBatch.id == batch_id,
    ).with_for_update().execution_options(populate_existing=True))
    if not batch:
        raise AuthorizationDrError("online_abc_batch_not_found", "Online ABC batch is unavailable")
    return batch


def _release_sha(value: str) -> str:
    normalized = value.strip().lower()
    if not SHA_PATTERN.fullmatch(normalized):
        raise AuthorizationDrError("runtime_image_mismatch", "Exact current release SHA is required")
    return normalized


def _key(value: str) -> str:
    normalized = value.strip()
    if not KEY_PATTERN.fullmatch(normalized):
        raise AuthorizationDrError("idempotency_key_required", "Recovery idempotency key is invalid")
    return normalized


def _fingerprint(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _audit_value(detail: str, key: str) -> str:
    match = re.search(rf"(?:^|; ){re.escape(key)}=([^;]*)(?:;|$)", detail or "")
    return match.group(1) if match else ""


__all__ = [
    "apply_deferred_recovery_start",
    "canonical_deferred_manifest",
    "deferred_recovery_status",
    "pause_deferred_recovery_for_error",
    "preview_deferred_recovery_start",
    "readback_deferred_recovery_start",
    "run_deferred_recovery_once",
]

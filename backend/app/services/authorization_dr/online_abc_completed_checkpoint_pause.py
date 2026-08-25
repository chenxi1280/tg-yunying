from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass

from sqlalchemy import func, select

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
from app.services._common import audit

from . import online_abc as abc
from .abc_verify import preview_abc_e4
from .contracts import AuthorizationDrError
from .online_abc import UNKNOWN_OPERATION_STATUSES
from .online_abc_manifest import ACTIVE_OPERATION_STATUSES
from .online_abc_operations import online_abc_item_operations
from .online_abc_primary import primary_state
from .online_abc_release_rebind import PAUSE_ACTION, PAUSE_BLOCKER
ACTION = "收口 ABC completed checkpoint 发布中断"
CLASSIFICATION = "completed_checkpoint_release_pause"
SHA_PATTERN = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
VALUE_PATTERN = re.compile(r"[A-Za-z0-9:._/-]{1,180}")
REMOTE_ID_PATTERN = re.compile(r"(?:^|; )primary_saved_message_id=([^;\s]+)")
ALLOWED_BATCH_ITEM_STATUSES = {"manual_required", "pending", "running", "succeeded"}
@dataclass(frozen=True)
class CompletedCheckpointContext:
    batch: TgAuthorizationOnlineAbcBatch
    item: TgAuthorizationOnlineAbcItem
    slots: dict[str, TgAuthorizationOnlineAbcSlotResult]
    operations: dict[str, TgAuthorizationDrOperation]
    account: TgAccount
    primary: TgAccountAuthorization
    e4_audit: AuditLog
def preview_completed_checkpoint_pause(
    session,
    batch_id: str,
    account_id: int,
    *,
    runtime_release_sha: str,
    idempotency_key: str,
    requested_by: str,
    approved_by: str,
    approval_ref: str,
    interruption_ref: str,
) -> dict:
    context = _context(session, batch_id, account_id)
    approval = _approval(
        context.batch, requested_by=requested_by,
        approved_by=approved_by, approval_ref=approval_ref,
    )
    release_sha = _runtime_release(context.batch, runtime_release_sha)
    key = _value(idempotency_key, "idempotency_key_required")
    interruption = _value(interruption_ref, "interruption_ref_invalid")
    counts = _require_boundary(session, context)
    payload = _payload(
        session, context, counts=counts, runtime_release_sha=release_sha,
        idempotency_key=key, approval=approval, interruption_ref=interruption,
    )
    return {**payload, "fingerprint": _fingerprint(payload)}
def apply_completed_checkpoint_pause(
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
    interruption_ref: str,
) -> dict:
    existing = _existing_result(
        session, batch_id, idempotency_key=idempotency_key,
        expected_fingerprint=expected_fingerprint,
    )
    if existing:
        return existing
    _lock_context(session, batch_id, account_id)
    preview = preview_completed_checkpoint_pause(
        session, batch_id, account_id, runtime_release_sha=runtime_release_sha,
        idempotency_key=idempotency_key, requested_by=requested_by,
        approved_by=approved_by, approval_ref=approval_ref,
        interruption_ref=interruption_ref,
    )
    if preview["fingerprint"] != expected_fingerprint:
        raise AuthorizationDrError("migration_fingerprint_conflict", "Completed checkpoint preview changed")
    context = _context(session, batch_id, account_id)
    _apply_projection(session, context, preview)
    session.commit()
    return _result(context.batch, context.item, preview["fingerprint"], already_applied=False)


def readback_completed_checkpoint_pause(
    session, batch_id: str, account_id: int, *, idempotency_key: str,
) -> dict:
    row = _existing_audit(session, batch_id, idempotency_key)
    if not row:
        raise AuthorizationDrError(
            "online_abc_completed_checkpoint_pause_not_found",
            "Completed checkpoint pause audit is unavailable",
        )
    item = _item(session, batch_id, account_id)
    fingerprint = _audit_value(row, "fingerprint")
    return _result(_batch(session, batch_id), item, fingerprint, already_applied=True)


def _context(session, batch_id: str, account_id: int) -> CompletedCheckpointContext:
    batch = _batch(session, batch_id)
    item = _item(session, batch_id, account_id)
    slots = _slots(session, item.id)
    operations = online_abc_item_operations(session, batch, item)
    account = session.get(TgAccount, item.account_id)
    primary = session.get(TgAccountAuthorization, item.primary_authorization_id)
    e4 = operations["e4"]
    e4_audit = _e4_audit(session, e4.id if e4 else "")
    if not account or not primary or not e4_audit:
        raise AuthorizationDrError(
            "online_abc_completed_checkpoint_missing", "Completed checkpoint facts are incomplete",
        )
    return CompletedCheckpointContext(batch, item, slots, operations, account, primary, e4_audit)


def _require_boundary(session, context: CompletedCheckpointContext) -> Counter:
    counts = _batch_boundary(session, context)
    _item_boundary(context)
    _operation_boundary(context)
    _primary_boundary(context)
    _global_boundary(session)
    return counts


def _batch_boundary(session, context: CompletedCheckpointContext) -> Counter:
    items = list(session.scalars(select(TgAuthorizationOnlineAbcItem).where(
        TgAuthorizationOnlineAbcItem.batch_id == context.batch.id,
    )))
    counts = Counter(item.status for item in items)
    valid = (
        context.batch.selection_mode == "all_online_accounts"
        and context.batch.status == "running"
        and len(items) == context.batch.target_count
        and counts["running"] == 1
        and counts["pending"] > 0
        and not set(counts) - ALLOWED_BATCH_ITEM_STATUSES
    )
    if not valid:
        raise AuthorizationDrError(
            "online_abc_completed_checkpoint_batch_invalid", "Batch boundary changed",
        )
    return counts


def _item_boundary(context: CompletedCheckpointContext) -> None:
    item = context.item
    slots = context.slots
    valid = (
        item.status == item.outcome == "running"
        and item.blocker_code == ""
        and item.finished_at is None
        and item.primary_probe_outcome == "pending"
        and set(slots) == {"standby_1", "standby_2"}
        and all(slot.outcome == "pending" for slot in slots.values())
        and all(slot.operation_id is None for slot in slots.values())
        and all(slot.blocker_code == "" for slot in slots.values())
    )
    if not valid:
        raise AuthorizationDrError(
            "online_abc_completed_checkpoint_item_invalid", "Item projection changed",
        )


def _operation_boundary(context: CompletedCheckpointContext) -> None:
    b_operation = context.operations["b"]
    c_operation = context.operations["c"]
    e4_operation = context.operations["e4"]
    valid = (
        b_operation
        and b_operation.operation_type == "provision_standby_1"
        and b_operation.status == b_operation.remote_call_state == "succeeded"
        and c_operation
        and c_operation.operation_type in {"migrate_standby_2", "provision_standby_2"}
        and c_operation.status == "succeeded"
        and c_operation.remote_call_state in {"confirmed", "succeeded"}
        and e4_operation
        and e4_operation.operation_type == "abc_e4_primary_send"
        and e4_operation.status == e4_operation.remote_call_state == "succeeded"
        and e4_operation.expected_current_authorization_id == context.primary.id
        and not any(operation.blocker_code for operation in context.operations.values())
    )
    if not valid or not _remote_message_id(context.e4_audit):
        raise AuthorizationDrError(
            "online_abc_completed_checkpoint_operation_invalid", "Completed operations changed",
        )


def _primary_boundary(context: CompletedCheckpointContext) -> None:
    if primary_state(context.account, context.primary, context.item) != "qualified":
        raise AuthorizationDrError("online_abc_primary_drift", "Frozen A changed")


def _global_boundary(session) -> None:
    runtime = session.get(AuthorizationDrRuntimeContract, 1)
    unknown = session.scalar(select(func.count()).select_from(TgAuthorizationDrOperation).where(
        TgAuthorizationDrOperation.status.in_(UNKNOWN_OPERATION_STATUSES),
    ))
    sensitive = session.scalar(select(func.count()).select_from(TgAuthorizationDrOperation).where(
        TgAuthorizationDrOperation.status.in_(ACTIVE_OPERATION_STATUSES),
    ))
    my_clients = session.scalar(select(func.coalesce(func.sum(
        AuthorizationDrExecutionNode.active_client_count,
    ), 0)).where(AuthorizationDrExecutionNode.region_code == "my"))
    valid = runtime and runtime.mode == "off" and not runtime.claim_scope_operation_id
    if not valid or unknown or sensitive or my_clients:
        raise AuthorizationDrError(
            "online_abc_completed_checkpoint_runtime_active", "Global boundary changed",
        )


def _payload(
    session,
    context: CompletedCheckpointContext,
    *,
    counts: Counter,
    runtime_release_sha: str,
    idempotency_key: str,
    approval: tuple[str, str, str],
    interruption_ref: str,
) -> dict:
    artifact = preview_abc_e4(
        session, context.batch.tenant_id, context.item.account_id,
        idempotency_key=f"{idempotency_key}:artifact-readback",
    )
    return {
        "batch_id": context.batch.id,
        "batch_version": context.batch.version,
        "previous_execution_release_sha": context.batch.execution_release_sha,
        "runtime_release_sha": runtime_release_sha,
        "item": _item_snapshot(context.item),
        "slots": _slot_snapshots(context.slots),
        "operations": _operation_snapshots(context.operations),
        "primary": _primary_snapshot(context),
        "artifact": artifact,
        "e4_audit_id": context.e4_audit.id,
        "e4_remote_message_id": _remote_message_id(context.e4_audit),
        "pending_count": counts["pending"],
        "manual_count": counts["manual_required"],
        "succeeded_count": counts["succeeded"],
        "classification": CLASSIFICATION,
        "blocker_code": PAUSE_BLOCKER,
        "idempotency_key": idempotency_key,
        "interruption_ref": interruption_ref,
        "requested_by": approval[0],
        "approved_by": approval[1],
        "approval_ref": approval[2],
    }


def _item_snapshot(item: TgAuthorizationOnlineAbcItem) -> list:
    return [
        item.id, item.account_id, item.ordinal, item.version, item.status,
        item.outcome, item.primary_probe_outcome,
    ]


def _slot_snapshots(slots: dict[str, TgAuthorizationOnlineAbcSlotResult]) -> list[list]:
    return [
        [name, slot.id, slot.version, slot.outcome, slot.operation_id, slot.blocker_code]
        for name, slot in sorted(slots.items())
    ]


def _operation_snapshots(operations: dict[str, TgAuthorizationDrOperation]) -> list[list]:
    return [
        [
            name, operation.id, operation.operation_version, operation.status,
            operation.remote_call_state, operation.candidate_authorization_id,
            operation.expected_current_authorization_id,
        ]
        for name, operation in sorted(operations.items())
    ]


def _primary_snapshot(context: CompletedCheckpointContext) -> list:
    account = context.account
    primary = context.primary
    return [
        primary.id, primary.fact_version, primary.status, primary.health_status,
        account.current_authorization_id == primary.id and primary.is_current,
        _digest(primary.session_ciphertext or ""),
        account.authorization_generation, account.authorization_fact_generation,
        account.connection_generation,
    ]


def _apply_projection(session, context: CompletedCheckpointContext, preview: dict) -> None:
    abc._sync_primary_probe(session, context.item)
    abc._sync_slot(session, context.item, "standby_1", context.operations["b"])
    abc._sync_slot(session, context.item, "standby_2", context.operations["c"])
    abc._complete_item(
        session, context.batch, context.item,
        preview["approved_by"], preview["approval_ref"],
    )
    if context.item.status != "succeeded":
        raise AuthorizationDrError(
            "online_abc_completed_checkpoint_projection_failed", "Item sync did not succeed",
        )
    context.batch.status = "stopped"
    context.batch.version += 1
    _audit_pause(session, context, preview)


def _audit_pause(session, context: CompletedCheckpointContext, preview: dict) -> None:
    counts = Counter(item.status for item in session.scalars(select(TgAuthorizationOnlineAbcItem).where(
        TgAuthorizationOnlineAbcItem.batch_id == context.batch.id,
    )))
    detail = (
        f"approval_ref={preview['approval_ref']}; blocker={PAUSE_BLOCKER}; "
        f"classification={CLASSIFICATION}; checkpoint_key={preview['idempotency_key']}; "
        f"fingerprint={preview['fingerprint']}; account_id={context.item.account_id}; "
        f"execution_release={preview['previous_execution_release_sha']}; "
        f"runtime_release={preview['runtime_release_sha']}; succeeded={counts['succeeded']}; "
        f"manual_required={counts['manual_required']}; pending={counts['pending']}; "
        "running_items=0; global_unknown=0; runtime=off"
    )
    audit(
        session, tenant_id=context.batch.tenant_id, actor=preview["approved_by"],
        action=PAUSE_ACTION, target_type="tg_authorization_online_abc_batches",
        target_id=context.batch.id, detail=detail,
    )


def _lock_context(session, batch_id: str, account_id: int) -> None:
    for model, conditions in (
        (TgAuthorizationOnlineAbcBatch, (TgAuthorizationOnlineAbcBatch.id == batch_id,)),
        (TgAuthorizationOnlineAbcItem, (
            TgAuthorizationOnlineAbcItem.batch_id == batch_id,
            TgAuthorizationOnlineAbcItem.account_id == account_id,
        )),
    ):
        session.scalar(select(model).where(*conditions).with_for_update().execution_options(
            populate_existing=True,
        ))
    context = _context(session, batch_id, account_id)
    _lock_rows(session, context)


def _lock_rows(session, context: CompletedCheckpointContext) -> None:
    for model, row_ids in (
        (TgAuthorizationOnlineAbcSlotResult, [slot.id for slot in context.slots.values()]),
        (TgAuthorizationDrOperation, [operation.id for operation in context.operations.values() if operation]),
        (TgAccount, [context.account.id]),
        (TgAccountAuthorization, [context.primary.id]),
    ):
        list(session.scalars(select(model).where(model.id.in_(row_ids)).with_for_update().execution_options(
            populate_existing=True,
        )))


def _existing_result(
    session, batch_id: str, *, idempotency_key: str, expected_fingerprint: str,
) -> dict | None:
    row = _existing_audit(session, batch_id, idempotency_key)
    if not row:
        return None
    fingerprint = _audit_value(row, "fingerprint")
    if fingerprint != expected_fingerprint:
        raise AuthorizationDrError(
            "migration_fingerprint_conflict", "Completed checkpoint fingerprint changed",
        )
    account_id = int(_audit_value(row, "account_id"))
    item = _item(session, batch_id, account_id)
    return _result(_batch(session, batch_id), item, fingerprint, already_applied=True)


def _existing_audit(session, batch_id: str, idempotency_key: str) -> AuditLog | None:
    key = _value(idempotency_key, "idempotency_key_required")
    return session.scalar(select(AuditLog).where(
        AuditLog.target_type == "tg_authorization_online_abc_batches",
        AuditLog.target_id == batch_id,
        AuditLog.action == PAUSE_ACTION,
        AuditLog.detail.contains(f"checkpoint_key={key};"),
    ).order_by(AuditLog.id.desc()).limit(1))


def _result(batch, item, fingerprint: str, *, already_applied: bool) -> dict:
    return {
        "batch_id": batch.id,
        "batch_status": batch.status,
        "batch_version": batch.version,
        "execution_release_sha": batch.execution_release_sha,
        "account_id": item.account_id,
        "item_status": item.status,
        "item_outcome": item.outcome,
        "item_version": item.version,
        "fingerprint": fingerprint,
        "already_applied": already_applied,
    }


def _batch(session, batch_id: str) -> TgAuthorizationOnlineAbcBatch:
    batch = session.get(TgAuthorizationOnlineAbcBatch, batch_id)
    if not batch:
        raise AuthorizationDrError("online_abc_batch_not_found", "Online ABC batch is unavailable")
    return batch


def _item(session, batch_id: str, account_id: int) -> TgAuthorizationOnlineAbcItem:
    item = session.scalar(select(TgAuthorizationOnlineAbcItem).where(
        TgAuthorizationOnlineAbcItem.batch_id == batch_id,
        TgAuthorizationOnlineAbcItem.account_id == account_id,
    ))
    if not item:
        raise AuthorizationDrError("online_abc_item_not_found", "Online ABC item is unavailable")
    return item


def _slots(session, item_id: str) -> dict[str, TgAuthorizationOnlineAbcSlotResult]:
    rows = session.scalars(select(TgAuthorizationOnlineAbcSlotResult).where(
        TgAuthorizationOnlineAbcSlotResult.item_id == item_id,
    ))
    return {row.logical_slot: row for row in rows}


def _e4_audit(session, operation_id: str) -> AuditLog | None:
    return session.scalar(select(AuditLog).where(
        AuditLog.target_type == "tg_authorization_dr_operation",
        AuditLog.target_id == operation_id,
        AuditLog.action == "完成 ABC canary E4",
    ).order_by(AuditLog.id.desc()).limit(1))


def _remote_message_id(row: AuditLog) -> str:
    match = REMOTE_ID_PATTERN.search(row.detail or "")
    return match.group(1) if match else ""


def _approval(
    batch, *, requested_by: str, approved_by: str, approval_ref: str,
) -> tuple[str, str, str]:
    values = tuple(value.strip() for value in (requested_by, approved_by, approval_ref))
    if not all(values) or values[0] == values[1]:
        raise AuthorizationDrError("approval_ref_required", "Distinct runner approval is required")
    if values != (batch.requested_by, batch.approved_by, batch.approval_ref):
        raise AuthorizationDrError("online_abc_runner_approval_mismatch", "Runner approval differs from batch")
    return values


def _runtime_release(batch, value: str) -> str:
    release_sha = value.strip().lower()
    previous = batch.execution_release_sha or batch.deployed_release_sha
    if not SHA_PATTERN.fullmatch(release_sha) or release_sha == previous:
        raise AuthorizationDrError("runtime_image_mismatch", "A distinct current release SHA is required")
    return release_sha


def _value(value: str, code: str) -> str:
    normalized = value.strip()
    if not VALUE_PATTERN.fullmatch(normalized):
        raise AuthorizationDrError(code, "Audit value is invalid")
    return normalized


def _audit_value(row: AuditLog, key: str) -> str:
    marker = f"{key}="
    for part in (row.detail or "").split("; "):
        if part.startswith(marker):
            return part[len(marker):]
    return ""


def _fingerprint(payload: dict) -> str:
    return _digest(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


__all__ = [
    "apply_completed_checkpoint_pause",
    "preview_completed_checkpoint_pause",
    "readback_completed_checkpoint_pause",
]

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter

from sqlalchemy import func, select

from app.models import (
    AuditLog,
    AuthorizationDrExecutionNode,
    AuthorizationDrRuntimeContract,
    TgAuthorizationDrOperation,
    TgAuthorizationOnlineAbcBatch,
    TgAuthorizationOnlineAbcItem,
)
from app.services._common import audit

from .contracts import AuthorizationDrError
from .online_abc import UNKNOWN_OPERATION_STATUSES
from .online_abc_operations import online_abc_item_operations
from .online_abc_primary import _primary_drifted
from .online_abc_read import item_operations_complete


PAUSE_ACTION = "生产版本变化暂停 ABC runner"
PAUSE_BLOCKER = "production_release_changed_mid_chunk"
REBIND_ACTION = "重绑 ABC runner 执行 release"
ALLOWED_ITEM_STATUSES = {"pending", "succeeded"}
SHA_PATTERN = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def preview_execution_release_rebind(
    session,
    batch_id: str,
    *,
    runtime_release_sha: str,
    requested_by: str,
    approved_by: str,
    approval_ref: str,
) -> dict:
    batch = _batch(session, batch_id)
    approval = _approval(batch, requested_by, approved_by, approval_ref)
    release_sha = _release_sha(batch, runtime_release_sha)
    payload = _preview_payload(session, batch, release_sha, approval)
    return {**payload, "fingerprint": _fingerprint(payload)}


def apply_execution_release_rebind(
    session,
    batch_id: str,
    *,
    runtime_release_sha: str,
    expected_fingerprint: str,
    requested_by: str,
    approved_by: str,
    approval_ref: str,
) -> dict:
    existing = _existing_result(session, batch_id, expected_fingerprint)
    if existing:
        return existing
    batch = _locked_batch(session, batch_id)
    preview = preview_execution_release_rebind(
        session,
        batch.id,
        runtime_release_sha=runtime_release_sha,
        requested_by=requested_by,
        approved_by=approved_by,
        approval_ref=approval_ref,
    )
    if preview["fingerprint"] != expected_fingerprint:
        raise AuthorizationDrError("migration_fingerprint_conflict", "Execution release rebind preview changed")
    previous_release_sha = batch.execution_release_sha or batch.deployed_release_sha
    batch.execution_release_sha = preview["runtime_release_sha"]
    batch.status = "running"
    batch.version += 1
    _audit_rebind(session, batch, preview, previous_release_sha, approved_by, approval_ref)
    session.commit()
    return _result(batch, preview["fingerprint"], already_applied=False)


def _preview_payload(session, batch, release_sha: str, approval: tuple[str, str, str]) -> dict:
    if batch.status != "stopped":
        raise AuthorizationDrError("online_abc_release_rebind_batch_not_stopped", "Batch is not stopped")
    items = list(session.scalars(select(TgAuthorizationOnlineAbcItem).where(
        TgAuthorizationOnlineAbcItem.batch_id == batch.id,
    ).order_by(TgAuthorizationOnlineAbcItem.ordinal)))
    counts = Counter(item.status for item in items)
    _require_item_boundary(session, batch, items, counts)
    pause = _require_release_pause_audit(session, batch, approval[2])
    _require_global_boundary(session)
    return {
        "batch_id": batch.id,
        "batch_version": batch.version,
        "deployed_release_sha": batch.deployed_release_sha,
        "previous_execution_release_sha": batch.execution_release_sha or batch.deployed_release_sha,
        "runtime_release_sha": release_sha,
        "pause_audit_id": pause.id,
        "succeeded_count": counts["succeeded"],
        "pending_count": counts["pending"],
        "requested_by": approval[0],
        "approved_by": approval[1],
        "approval_ref": approval[2],
    }


def _require_item_boundary(session, batch, items, counts: Counter) -> None:
    if len(items) != batch.target_count or not items or not counts["pending"]:
        raise AuthorizationDrError("online_abc_release_rebind_item_boundary", "Frozen item conservation failed")
    if set(counts) - ALLOWED_ITEM_STATUSES:
        raise AuthorizationDrError("online_abc_release_rebind_item_boundary", "Items are not quiescent")
    for item in items:
        if item.status != "succeeded":
            continue
        if _primary_drifted(session, item):
            raise AuthorizationDrError("online_abc_primary_drift", "A completed canary primary drifted")
        operations = online_abc_item_operations(session, batch, item)
        if not item_operations_complete(session, item, operations):
            raise AuthorizationDrError("online_abc_release_rebind_completed_incomplete", "Completed item facts are incomplete")


def _require_global_boundary(session) -> None:
    contract = session.get(AuthorizationDrRuntimeContract, 1)
    if not contract or contract.mode != "off" or contract.claim_scope_operation_id:
        raise AuthorizationDrError("runtime_capability_unproven", "DR runtime is not safely off")
    unknown = session.scalar(select(func.count()).select_from(TgAuthorizationDrOperation).where(
        TgAuthorizationDrOperation.status.in_(UNKNOWN_OPERATION_STATUSES),
    ))
    if unknown:
        raise AuthorizationDrError("global_reconcile_unknown", "Global reconcile unknown must be zero")
    my_clients = session.scalar(select(func.coalesce(func.sum(AuthorizationDrExecutionNode.active_client_count), 0)).where(
        AuthorizationDrExecutionNode.region_code == "my",
    ))
    if my_clients:
        raise AuthorizationDrError("malaysia_client_leak", "Malaysia active client count must be zero")


def _require_release_pause_audit(session, batch, approval_ref: str):
    row = session.scalar(select(AuditLog).where(
        AuditLog.target_type == "tg_authorization_online_abc_batches",
        AuditLog.target_id == batch.id,
    ).order_by(AuditLog.id.desc()).limit(1))
    expected_ref = f"approval_ref={approval_ref}"
    if not row or row.action != PAUSE_ACTION or PAUSE_BLOCKER not in row.detail or expected_ref not in row.detail:
        raise AuthorizationDrError("online_abc_release_rebind_pause_unproven", "Release pause audit is unavailable")
    return row


def _approval(batch, requested_by: str, approved_by: str, approval_ref: str) -> tuple[str, str, str]:
    values = tuple(value.strip() for value in (requested_by, approved_by, approval_ref))
    if not all(values) or values[0] == values[1]:
        raise AuthorizationDrError("approval_ref_required", "Distinct runner approval is required")
    if values != (batch.requested_by, batch.approved_by, batch.approval_ref):
        raise AuthorizationDrError("online_abc_runner_approval_mismatch", "Runner approval differs from batch")
    return values


def _release_sha(batch, runtime_release_sha: str) -> str:
    value = runtime_release_sha.strip().lower()
    previous = batch.execution_release_sha or batch.deployed_release_sha
    if not SHA_PATTERN.fullmatch(value) or value == previous:
        raise AuthorizationDrError("runtime_image_mismatch", "A distinct current release SHA is required")
    return value


def _batch(session, batch_id: str):
    batch = session.get(TgAuthorizationOnlineAbcBatch, batch_id)
    if not batch:
        raise AuthorizationDrError("online_abc_batch_not_found", "Online ABC batch is unavailable")
    return batch


def _locked_batch(session, batch_id: str):
    batch = session.scalar(select(TgAuthorizationOnlineAbcBatch).where(
        TgAuthorizationOnlineAbcBatch.id == batch_id,
    ).with_for_update())
    if not batch:
        raise AuthorizationDrError("online_abc_batch_not_found", "Online ABC batch is unavailable")
    return batch


def _fingerprint(payload: dict) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()


def _audit_rebind(session, batch, preview, previous_sha, actor: str, approval_ref: str) -> None:
    audit(
        session,
        tenant_id=batch.tenant_id,
        actor=actor,
        action=REBIND_ACTION,
        target_type="tg_authorization_online_abc_batches",
        target_id=batch.id,
        detail=(
            f"approval_ref={approval_ref}; fingerprint={preview['fingerprint']}; "
            f"pause_audit_id={preview['pause_audit_id']}; execution_release={previous_sha}->"
            f"{preview['runtime_release_sha']}; succeeded={preview['succeeded_count']}; "
            f"pending={preview['pending_count']}"
        ),
    )


def _existing_result(session, batch_id: str, fingerprint: str) -> dict | None:
    if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        return None
    row = session.scalar(select(AuditLog).where(
        AuditLog.target_type == "tg_authorization_online_abc_batches",
        AuditLog.target_id == batch_id,
        AuditLog.action == REBIND_ACTION,
        AuditLog.detail.contains(f"fingerprint={fingerprint}"),
    ).order_by(AuditLog.id.desc()).limit(1))
    return _result(_batch(session, batch_id), fingerprint, already_applied=True) if row else None


def _result(batch, fingerprint: str, *, already_applied: bool) -> dict:
    return {
        "batch_id": batch.id,
        "batch_status": batch.status,
        "batch_version": batch.version,
        "deployed_release_sha": batch.deployed_release_sha,
        "execution_release_sha": batch.execution_release_sha,
        "fingerprint": fingerprint,
        "already_applied": already_applied,
    }


__all__ = ["apply_execution_release_rebind", "preview_execution_release_rebind"]

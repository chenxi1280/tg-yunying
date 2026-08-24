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
    TgAccount,
    TgAccountAuthorization,
    TgAuthorizationDrBatch,
    TgAuthorizationDrOperation,
    TgAuthorizationOnlineAbcBatch,
    TgAuthorizationOnlineAbcItem,
    TgAuthorizationOnlineAbcSlotResult,
)
from app.services._common import audit

from .contracts import AuthorizationDrError
from .online_abc import UNKNOWN_OPERATION_STATUSES
from .online_abc_manifest import ACTIVE_OPERATION_STATUSES, _freeze_full_target
from .online_abc_operations import online_abc_operation_keys
from .online_abc_primary import primary_state
from .online_abc_release_rebind import REBIND_ACTION


AUDIT_ACTION = "重基线 pending ABC B/C plan"
IDEMPOTENCY_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,99}")
MUTABLE_FIELDS = (
    "app_b_id",
    "app_b_credentials_version",
    "app_b_assignment_purpose",
    "app_b_assignment_version",
    "proxy_id",
    "standby_1_plan",
    "standby_2_plan",
)


def preview_pending_plan_rebase(
    session,
    batch_id: str,
    *,
    expected_target_count: int,
    idempotency_key: str,
    requested_by: str,
    approved_by: str,
    approval_ref: str,
) -> dict:
    batch = _batch(session, batch_id)
    approval = _approval(batch, requested_by, approved_by, approval_ref)
    items = _items(session, batch.id, for_update=False)
    payload = _preview_payload(
        session, batch, items, expected_target_count, idempotency_key, approval,
    )
    return {**payload, "fingerprint": _fingerprint(payload)}


def apply_pending_plan_rebase(
    session,
    batch_id: str,
    *,
    expected_target_count: int,
    idempotency_key: str,
    expected_fingerprint: str,
    requested_by: str,
    approved_by: str,
    approval_ref: str,
) -> dict:
    batch = _batch(session, batch_id)
    approval = _approval(batch, requested_by, approved_by, approval_ref)
    existing = _existing_audit(session, batch.id, idempotency_key)
    if existing:
        return _existing_result(existing, expected_fingerprint, expected_target_count, batch)
    session.expire_all()
    batch = _locked_batch(session, batch.id)
    existing = _existing_audit(session, batch.id, idempotency_key)
    if existing:
        return _existing_result(existing, expected_fingerprint, expected_target_count, batch)
    items = _items(session, batch.id, for_update=True)
    payload = _preview_payload(
        session, batch, items, expected_target_count, idempotency_key, approval,
    )
    fingerprint = _fingerprint(payload)
    if fingerprint != expected_fingerprint:
        raise AuthorizationDrError("migration_fingerprint_conflict", "Pending plan rebase preview changed")
    _apply_targets(items, payload["targets"])
    _audit_apply(session, batch, payload, fingerprint, approval)
    session.commit()
    return _result(batch, payload, fingerprint, already_applied=False)


def _preview_payload(session, batch, items, expected_count, idempotency_key, approval) -> dict:
    _require_batch_boundary(session, batch, items)
    _require_global_boundary(session)
    key = _idempotency_key(idempotency_key)
    operation_keys = _remote_effect_keys(session, batch)
    slots = _slot_map(session, batch.id)
    targets, reasons = _collect_targets(session, batch, items, operation_keys, slots)
    _require_target_count(targets, reasons, expected_count)
    account_ids = [target["account_id"] for target in targets]
    return {
        "batch_id": batch.id,
        "batch_version": batch.version,
        "batch_status": batch.status,
        "expected_target_count": expected_count,
        "target_count": len(targets),
        "target_account_ids": account_ids,
        "target_set_fingerprint": _fingerprint(account_ids),
        "targets": targets,
        "idempotency_key": key,
        "requested_by": approval[0],
        "approved_by": approval[1],
        "approval_ref": approval[2],
    }


def _collect_targets(session, batch, items, operation_keys, slots) -> tuple[list[dict], Counter]:
    targets: list[dict] = []
    reasons: Counter = Counter()
    for item in items:
        target, reason = _candidate(session, batch, item, operation_keys, slots)
        reasons[reason] += 1
        if target:
            targets.append(target)
    return targets, reasons


def _candidate(session, batch, item, operation_keys, slots) -> tuple[dict | None, str]:
    if not _pending_item(item):
        return None, "not_pending"
    if not _stale_projection(item):
        return None, "already_current"
    if _remote_effect_started(batch, item, operation_keys):
        return None, "remote_effect_started"
    if not _pending_slots(slots.get(item.id, [])):
        return None, "slot_not_pending"
    account = session.get(TgAccount, item.account_id)
    primary = session.get(TgAccountAuthorization, item.primary_authorization_id)
    if primary_state(account, primary, item) not in {"frozen", "legacy_frozen"}:
        return None, "primary_drift"
    current = _freeze_full_target(session, batch.tenant_id, item.account_id)
    if not _immutable_projection_matches(item, current):
        return None, "immutable_projection_drift"
    if "blocked" in {current["standby_1_plan"], current["standby_2_plan"]}:
        return None, "still_blocked"
    if _mutable_projection(item) == _mutable_projection(current):
        return None, "already_current"
    return _target(item, current), "eligible"


def _target(item, current: dict) -> dict:
    return {
        "item_id": item.id,
        "item_version": item.version,
        "account_id": item.account_id,
        "primary": {
            "authorization_id": item.primary_authorization_id,
            "fact_version": item.primary_fact_version,
            "authorization_generation": item.authorization_generation,
            "authorization_fact_generation": item.authorization_fact_generation,
            "connection_generation": item.connection_generation,
            "session_digest": item.primary_session_digest,
        },
        "source_c": {
            "authorization_id": item.source_c_authorization_id,
            "fact_version": item.source_c_fact_version,
            "slot_generation": item.source_c_slot_generation,
        },
        "old": _mutable_projection(item),
        "new": _mutable_projection(current),
    }


def _pending_item(item) -> bool:
    return bool(
        item.status == "pending"
        and item.outcome == "pending"
        and item.blocker_code == ""
        and item.started_at is None
        and item.finished_at is None
    )


def _stale_projection(item) -> bool:
    return "blocked" in {item.standby_1_plan, item.standby_2_plan}


def _pending_slots(slots) -> bool:
    return bool(
        {slot.logical_slot for slot in slots} == {"standby_1", "standby_2"}
        and all(
            slot.outcome == "pending" and slot.operation_id is None and slot.blocker_code == ""
            for slot in slots
        )
    )


def _immutable_projection_matches(item, current: dict) -> bool:
    expected = (
        item.primary_authorization_id,
        item.primary_fact_version,
        item.authorization_generation,
        item.authorization_fact_generation,
        item.connection_generation,
        item.primary_session_digest,
        item.source_c_authorization_id,
        item.source_c_fact_version,
        item.source_c_slot_generation,
    )
    actual = (
        current["primary_authorization_id"],
        current["primary_fact_version"],
        current["authorization_generation"],
        current["authorization_fact_generation"],
        current["connection_generation"],
        current["primary_session_digest"],
        current["source_c_authorization_id"],
        current["source_c_fact_version"],
        current["source_c_slot_generation"],
    )
    return expected == actual


def _mutable_projection(value) -> dict:
    return {field: _value(value, field) for field in MUTABLE_FIELDS}


def _value(value, field: str):
    return value[field] if isinstance(value, dict) else getattr(value, field)


def _remote_effect_keys(session, batch) -> tuple[set[str], set[str]]:
    prefix = f"online-abc:{batch.id}:"
    operation_keys = set(session.scalars(select(TgAuthorizationDrOperation.idempotency_key).where(
        TgAuthorizationDrOperation.tenant_id == batch.tenant_id,
        TgAuthorizationDrOperation.idempotency_key.like(f"{prefix}%"),
    )))
    batch_keys = set(session.scalars(select(TgAuthorizationDrBatch.idempotency_key).where(
        TgAuthorizationDrBatch.tenant_id == batch.tenant_id,
        TgAuthorizationDrBatch.idempotency_key.like(f"{prefix}%"),
    )))
    return operation_keys, batch_keys


def _remote_effect_started(batch, item, frozen_keys) -> bool:
    operation_keys, batch_keys = frozen_keys
    keys = online_abc_operation_keys(batch, item)
    has_operation = keys["b"] in operation_keys or any(
        key.startswith(keys["e4"]) for key in operation_keys
    )
    return has_operation or any(key.startswith(keys["c"]) for key in batch_keys)


def _slot_map(session, batch_id: str) -> dict[str, list]:
    rows = list(session.scalars(select(TgAuthorizationOnlineAbcSlotResult).where(
        TgAuthorizationOnlineAbcSlotResult.batch_id == batch_id,
    )))
    result: dict[str, list] = {}
    for row in rows:
        result.setdefault(row.item_id, []).append(row)
    return result


def _require_batch_boundary(session, batch, items) -> None:
    if batch.status == "running":
        _require_release_rebound(session, batch)
    elif batch.status != "stopped":
        raise AuthorizationDrError("online_abc_pending_rebase_batch_not_stopped", "Batch is not stopped")
    if len(items) != batch.target_count:
        raise AuthorizationDrError("online_abc_pending_rebase_conservation", "Frozen item count changed")
    active = [item for item in items if item.status not in {"pending", "succeeded"}]
    if active:
        raise AuthorizationDrError("online_abc_pending_rebase_item_active", "Batch has non-quiescent items")


def _require_release_rebound(session, batch) -> None:
    row = session.scalar(select(AuditLog).where(
        AuditLog.target_type == "tg_authorization_online_abc_batches",
        AuditLog.target_id == batch.id,
    ).order_by(AuditLog.id.desc()).limit(1))
    tokens = (
        f"approval_ref={batch.approval_ref};",
        f"->{batch.execution_release_sha};",
    )
    valid = bool(
        row and row.action == REBIND_ACTION and batch.execution_release_sha
        and all(token in row.detail for token in tokens)
    )
    if not valid:
        raise AuthorizationDrError(
            "online_abc_pending_rebase_running_unproven",
            "Running batch is not a quiescent release rebind",
        )


def _require_global_boundary(session) -> None:
    runtime = session.get(AuthorizationDrRuntimeContract, 1)
    if not runtime or runtime.mode != "off" or runtime.claim_scope_operation_id:
        raise AuthorizationDrError("runtime_capability_unproven", "DR runtime must be safely off")
    unknown = session.scalar(select(TgAuthorizationDrOperation.id).where(
        TgAuthorizationDrOperation.status.in_(UNKNOWN_OPERATION_STATUSES),
    ).limit(1))
    if unknown:
        raise AuthorizationDrError("global_reconcile_unknown", "Global reconcile unknown must be zero")
    sensitive = session.scalar(select(TgAuthorizationDrOperation.id).where(
        TgAuthorizationDrOperation.status.in_(ACTIVE_OPERATION_STATUSES),
    ).limit(1))
    if sensitive:
        raise AuthorizationDrError("online_abc_sensitive_operation", "Sensitive operation is active")
    my_clients = session.scalar(select(func.coalesce(func.sum(AuthorizationDrExecutionNode.active_client_count), 0)).where(
        AuthorizationDrExecutionNode.region_code == "my",
    ))
    if my_clients:
        raise AuthorizationDrError("malaysia_client_leak", "Malaysia active client count must be zero")


def _require_target_count(targets, reasons: Counter, expected_count: int) -> None:
    if expected_count <= 0 or len(targets) != expected_count:
        summary = ",".join(f"{key}={value}" for key, value in sorted(reasons.items()))
        raise AuthorizationDrError(
            "online_abc_pending_rebase_target_count",
            f"Pending plan targets changed: eligible={len(targets)} expected={expected_count}; {summary}",
        )


def _approval(batch, requested_by: str, approved_by: str, approval_ref: str) -> tuple[str, str, str]:
    values = tuple(value.strip() for value in (requested_by, approved_by, approval_ref))
    if not all(values) or values[0] == values[1]:
        raise AuthorizationDrError("approval_ref_required", "Distinct rebase approval is required")
    if values != (batch.requested_by, batch.approved_by, batch.approval_ref):
        raise AuthorizationDrError("online_abc_runner_approval_mismatch", "Rebase approval differs from batch")
    return values


def _idempotency_key(value: str) -> str:
    key = value.strip()
    if not IDEMPOTENCY_PATTERN.fullmatch(key):
        raise AuthorizationDrError("idempotency_key_required", "Pending plan rebase key is required")
    return key


def _batch(session, batch_id: str):
    batch = session.get(TgAuthorizationOnlineAbcBatch, batch_id)
    if not batch:
        raise AuthorizationDrError("online_abc_batch_not_found", "Online ABC batch is unavailable")
    return batch


def _locked_batch(session, batch_id: str):
    batch = session.scalar(select(TgAuthorizationOnlineAbcBatch).where(
        TgAuthorizationOnlineAbcBatch.id == batch_id,
    ).with_for_update().execution_options(populate_existing=True))
    if not batch:
        raise AuthorizationDrError("online_abc_batch_not_found", "Online ABC batch is unavailable")
    return batch


def _items(session, batch_id: str, *, for_update: bool) -> list:
    query = select(TgAuthorizationOnlineAbcItem).where(
        TgAuthorizationOnlineAbcItem.batch_id == batch_id,
    ).order_by(TgAuthorizationOnlineAbcItem.ordinal)
    if for_update:
        query = query.with_for_update().execution_options(populate_existing=True)
    return list(session.scalars(query))


def _apply_targets(items, targets: list[dict]) -> None:
    by_id = {item.id: item for item in items}
    for target in targets:
        item = by_id[target["item_id"]]
        if item.version != target["item_version"]:
            raise AuthorizationDrError("migration_fingerprint_conflict", "Pending item version changed")
        for field in MUTABLE_FIELDS:
            setattr(item, field, target["new"][field])
        item.version += 1


def _audit_apply(session, batch, payload, fingerprint: str, approval) -> None:
    audit(
        session,
        tenant_id=batch.tenant_id,
        actor=approval[1],
        action=AUDIT_ACTION,
        target_type="tg_authorization_online_abc_batches",
        target_id=batch.id,
        detail=(
            f"idempotency_key={payload['idempotency_key']}; fingerprint={fingerprint}; "
            f"target_set_fingerprint={payload['target_set_fingerprint']}; "
            f"target_count={payload['target_count']}; approval_ref={approval[2]}"
        ),
    )


def _existing_audit(session, batch_id: str, idempotency_key: str):
    key = _idempotency_key(idempotency_key)
    token = f"idempotency_key={key};"
    rows = session.scalars(select(AuditLog).where(
        AuditLog.action == AUDIT_ACTION,
        AuditLog.target_type == "tg_authorization_online_abc_batches",
        AuditLog.target_id == batch_id,
    ).order_by(AuditLog.id.desc()))
    return next((row for row in rows if token in row.detail), None)


def _existing_result(row, fingerprint: str, expected_count: int, batch) -> dict:
    valid = (
        re.fullmatch(r"[0-9a-f]{64}", fingerprint)
        and f"fingerprint={fingerprint};" in row.detail
        and f"target_count={expected_count};" in row.detail
    )
    if not valid:
        raise AuthorizationDrError("migration_fingerprint_conflict", "Pending plan rebase key changed")
    return {
        "batch_id": batch.id,
        "batch_status": batch.status,
        "batch_version": batch.version,
        "target_count": expected_count,
        "fingerprint": fingerprint,
        "already_applied": True,
    }


def _result(batch, payload, fingerprint: str, *, already_applied: bool) -> dict:
    return {
        "batch_id": batch.id,
        "batch_status": batch.status,
        "batch_version": batch.version,
        "target_count": payload["target_count"],
        "target_account_ids": payload["target_account_ids"],
        "target_set_fingerprint": payload["target_set_fingerprint"],
        "fingerprint": fingerprint,
        "already_applied": already_applied,
    }


def _fingerprint(payload) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()


__all__ = ["apply_pending_plan_rebase", "preview_pending_plan_rebase"]

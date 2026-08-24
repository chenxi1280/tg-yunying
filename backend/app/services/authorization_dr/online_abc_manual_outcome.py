from __future__ import annotations

import hashlib
import json
import re
from collections import Counter

from sqlalchemy import func, select

from app.models import (
    AccountStatus,
    AuditLog,
    AuthorizationDrExecutionNode,
    AuthorizationDrRuntimeContract,
    TgAccount,
    TgAccountAuthorization,
    TgAuthorizationDrOperation,
    TgAuthorizationOnlineAbcBatch,
    TgAuthorizationOnlineAbcItem,
    TgAuthorizationOnlineAbcSlotResult,
    TgLoginFlow,
)
from app.services._common import _now, audit

from .contracts import AuthorizationDrError
from .online_abc import UNKNOWN_OPERATION_STATUSES
from .online_abc_manifest import ACTIVE_OPERATION_STATUSES
from .online_abc_operations import online_abc_item_operations
from .online_abc_primary import primary_state


MANUAL_ACTION = "确认 ABC full item 人工失败并继续"
MANUAL_OUTCOME = "manual_required"
UPSTREAM_MANUAL_BLOCKER = "upstream_b_manual_required"
UNREADABLE_CODE_BLOCKER = "verification_code_unreadable"
SHA_PATTERN = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
KEY_PATTERN = re.compile(r"[A-Za-z0-9:._-]{1,100}")


def preview_manual_online_abc_outcome(
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
    batch = _batch(session, batch_id)
    approval = _approval(
        batch, requested_by=requested_by, approved_by=approved_by, approval_ref=approval_ref,
    )
    release_sha = _release_sha(runtime_release_sha)
    key = _idempotency_key(idempotency_key)
    item = _item(session, batch, account_id)
    slots = _slots(session, item)
    operations = online_abc_item_operations(session, batch, item)
    _require_batch_boundary(session, batch, item)
    manual_stage, operation = _manual_context(session, item, slots=slots, operations=operations)
    global_state = _require_global_boundary(session)
    primary = _primary_snapshot(session, item)
    counts = Counter(value.status for value in _items(session, batch))
    payload = _preview_payload(
        batch=batch,
        item=item,
        slots=slots,
        operation=operation,
        manual_stage=manual_stage,
        primary=primary,
        global_state=global_state,
        counts=counts,
        release_sha=release_sha,
        key=key,
        approval=approval,
    )
    return {**payload, "fingerprint": _fingerprint(payload)}


def apply_manual_online_abc_outcome(
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
    existing = _existing_result(session, batch_id, account_id, idempotency_key)
    if existing:
        return _idempotent_result(existing, expected_fingerprint)
    _locked_batch(session, batch_id)
    _locked_item(session, batch_id, account_id)
    existing = _existing_result(session, batch_id, account_id, idempotency_key)
    if existing:
        return _idempotent_result(existing, expected_fingerprint)
    preview = preview_manual_online_abc_outcome(
        session, batch_id, account_id,
        runtime_release_sha=runtime_release_sha,
        idempotency_key=idempotency_key,
        requested_by=requested_by,
        approved_by=approved_by,
        approval_ref=approval_ref,
    )
    if preview["fingerprint"] != expected_fingerprint:
        raise AuthorizationDrError("migration_fingerprint_conflict", "Manual outcome preview changed")
    _apply_transition(session, preview)
    session.commit()
    result = _result(session, batch_id, account_id, fingerprint=preview["fingerprint"])
    return {**result, "already_applied": False}


def read_manual_online_abc_outcome(
    session, batch_id: str, account_id: int, *, idempotency_key: str,
) -> dict:
    result = _existing_result(session, batch_id, account_id, idempotency_key)
    if not result:
        raise AuthorizationDrError("online_abc_manual_outcome_not_found", "Manual outcome audit is unavailable")
    return {**result, "already_applied": True}


def _preview_payload(
    *, batch, item, slots, operation, manual_stage, primary, global_state, counts,
    release_sha: str, key: str, approval: tuple[str, str, str],
) -> dict:
    return {
        "batch_id": batch.id,
        "batch_version": batch.version,
        "selection_mode": batch.selection_mode,
        "account_id": item.account_id,
        "item_id": item.id,
        "item_version": item.version,
        "item_outcome": item.outcome,
        "b_slot_id": slots["standby_1"].id,
        "b_slot_version": slots["standby_1"].version,
        "c_slot_id": slots["standby_2"].id,
        "c_slot_version": slots["standby_2"].version,
        "manual_stage": manual_stage,
        "manual_operation_id": operation.id,
        "manual_operation_version": operation.operation_version,
        "manual_operation_status": operation.status,
        "manual_remote_call_state": operation.remote_call_state,
        "manual_reconcile_status": operation.reconcile_status,
        "manual_blocker_code": operation.blocker_code,
        "b_slot_outcome": slots["standby_1"].outcome,
        "c_slot_outcome": slots["standby_2"].outcome,
        "primary": primary,
        "global": global_state,
        "previous_execution_release_sha": batch.execution_release_sha or batch.deployed_release_sha,
        "runtime_release_sha": release_sha,
        "pending_count": counts["pending"],
        "manual_count": counts[MANUAL_OUTCOME],
        "idempotency_key": key,
        "requested_by": approval[0],
        "approved_by": approval[1],
        "approval_ref": approval[2],
    }


def _items(session, batch):
    return list(session.scalars(select(TgAuthorizationOnlineAbcItem).where(
        TgAuthorizationOnlineAbcItem.batch_id == batch.id,
    )))


def _require_batch_boundary(session, batch, item) -> None:
    items = _items(session, batch)
    statuses = Counter(value.status for value in items)
    valid_statuses = {"pending", "succeeded", MANUAL_OUTCOME, "stopped"}
    valid = (
        batch.selection_mode == "all_online_accounts"
        and batch.status == "stopped"
        and len(items) == batch.target_count
        and statuses["stopped"] == 1
        and item.status == "stopped"
        and not statuses["running"]
        and bool(statuses["pending"])
        and not set(statuses) - valid_statuses
    )
    if not valid:
        raise AuthorizationDrError("online_abc_manual_outcome_batch_invalid", "Full batch boundary changed")


def _manual_context(session, item, *, slots: dict, operations: dict) -> tuple[str, object]:
    if _manual_b_valid(session, item, slots=slots, operations=operations):
        return "b", operations["b"]
    if _manual_c_valid(item, slots, operations):
        return "c", operations["c"]
    raise AuthorizationDrError("online_abc_manual_outcome_state_invalid", "Manual terminal state changed")


def _manual_b_valid(session, item, *, slots: dict, operations: dict) -> bool:
    operation = operations["b"]
    b_slot = slots["standby_1"]
    c_slot = slots["standby_2"]
    return bool(
        _manual_b_terminal(session, item, slot=b_slot, operation=operation)
        and item.primary_probe_outcome == "pending"
        and b_slot.operation_id == operation.id
        and c_slot.outcome == "pending"
        and c_slot.operation_id is None
        and operations["c"] is None
        and operations["e4"] is None
    )


def _manual_b_terminal(session, item, *, slot, operation) -> bool:
    if _failed_b_without_code(session, operation):
        return item.outcome == "failed" and slot.outcome == "failed"
    return bool(
        item.outcome in {"reconcile_unknown", MANUAL_OUTCOME}
        and slot.outcome in {"reconcile_unknown", MANUAL_OUTCOME}
        and _confirmed_manual(operation)
    )


def _failed_b_without_code(session, operation) -> bool:
    flow = session.get(TgLoginFlow, operation.login_flow_id) if operation and operation.login_flow_id else None
    return bool(
        operation
        and operation.operation_type == "provision_standby_1"
        and operation.status == "failed"
        and operation.remote_call_state == "started"
        and operation.blocker_code == UNREADABLE_CODE_BLOCKER
        and operation.remote_effect_started_at
        and operation.login_challenge_sent_at
        and not operation.login_code_message_id
        and not operation.login_code_received_at
        and not operation.candidate_authorization_id
        and flow
        and flow.tenant_id == operation.tenant_id
        and flow.account_id == operation.account_id
        and flow.authorization_role == "standby_1"
        and flow.developer_app_id == operation.developer_app_id
        and flow.status == AccountStatus.WAITING_CODE.value
        and flow.challenge_sent_at
        and flow.code_expires_at
        and flow.authorization_id is None
        and flow.superseded_by_flow_id is None
    )


def _manual_c_valid(item, slots: dict, operations: dict) -> bool:
    operation = operations["c"]
    c_slot = slots["standby_2"]
    return bool(
        item.outcome == MANUAL_OUTCOME
        and item.primary_probe_outcome == "succeeded"
        and _b_stage_ready(item, slots["standby_1"], operations["b"])
        and _confirmed_manual(operation)
        and c_slot.operation_id == operation.id
        and c_slot.outcome == MANUAL_OUTCOME
        and operations["e4"] is None
    )


def _b_stage_ready(item, slot, operation) -> bool:
    if item.standby_1_plan == "already_qualified":
        return slot.outcome == "already_qualified" and operation is None
    return bool(slot.outcome == "succeeded" and operation and operation.status == "succeeded")


def _confirmed_manual(operation) -> bool:
    return bool(
        operation
        and operation.status == MANUAL_OUTCOME
        and operation.remote_call_state == "confirmed_no_effect"
        and operation.blocker_code
    )


def _require_global_boundary(session) -> dict:
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
        raise AuthorizationDrError("online_abc_manual_outcome_runtime_active", "Global DR boundary is not quiescent")
    return {"runtime_mode": runtime.mode, "runtime_scope": "", "unknown": 0, "sensitive": 0, "my_clients": 0}


def _primary_snapshot(session, item) -> dict:
    account = session.get(TgAccount, item.account_id)
    primary = session.get(TgAccountAuthorization, item.primary_authorization_id)
    state = primary_state(account, primary, item) if account and primary else "drifted"
    if state not in {"frozen", "legacy_frozen", "qualified"}:
        raise AuthorizationDrError("online_abc_primary_drift", "A changed before manual outcome")
    return {
        "authorization_id": primary.id,
        "state": state,
        "account_status": account.status,
        "account_developer_app_id": account.developer_app_id,
        "authorization_status": primary.status,
        "health_status": primary.health_status,
        "authorization_developer_app_id": primary.developer_app_id,
        "logical_slot": primary.logical_slot,
        "is_current": primary.is_current,
        "is_slot_current": primary.is_slot_current,
        "fact_version": primary.fact_version,
        "session_digest": hashlib.sha256((primary.session_ciphertext or "").encode()).hexdigest(),
        "authorization_generation": account.authorization_generation,
        "authorization_fact_generation": account.authorization_fact_generation,
        "connection_generation": account.connection_generation,
    }


def _apply_transition(session, preview: dict) -> None:
    batch = _batch(session, preview["batch_id"])
    item = _item(session, batch, preview["account_id"])
    slots = _slots(session, item)
    b_slot = slots["standby_1"]
    c_slot = slots["standby_2"]
    if preview["manual_stage"] == "b":
        _mark_slot_manual(b_slot, preview["manual_blocker_code"])
        _mark_slot_manual(c_slot, UPSTREAM_MANUAL_BLOCKER)
    else:
        _mark_slot_manual(c_slot, preview["manual_blocker_code"])
    item.status = MANUAL_OUTCOME
    item.outcome = MANUAL_OUTCOME
    item.blocker_code = preview["manual_blocker_code"]
    item.finished_at = _now()
    item.version += 1
    batch.execution_release_sha = preview["runtime_release_sha"]
    batch.status = "running" if preview["pending_count"] else "completed_with_manual"
    batch.version += 1
    _audit_transition(session, batch, item, preview)


def _mark_slot_manual(slot, blocker_code: str) -> None:
    if slot.outcome == MANUAL_OUTCOME and slot.blocker_code == blocker_code:
        return
    slot.outcome = MANUAL_OUTCOME
    slot.blocker_code = blocker_code
    slot.version += 1


def _audit_transition(session, batch, item, preview: dict) -> None:
    audit(
        session,
        tenant_id=batch.tenant_id,
        actor=preview["approved_by"],
        action=MANUAL_ACTION,
        target_type="tg_authorization_online_abc_items",
        target_id=item.id,
        detail=(
            f"account_id={item.account_id}; approval_ref={preview['approval_ref']}; "
            f"idempotency_key={preview['idempotency_key']}; fingerprint={preview['fingerprint']}; "
            f"stage={preview['manual_stage']}; blocker={preview['manual_blocker_code']}; "
            f"operation={preview['manual_operation_id']}; "
            f"batch_version={preview['batch_version']}->{batch.version}; "
            f"item_version={preview['item_version']}->{item.version}; "
            f"execution_release={preview['previous_execution_release_sha']}->{preview['runtime_release_sha']}"
        ),
    )


def _existing_result(session, batch_id: str, account_id: int, key: str) -> dict | None:
    normalized = _idempotency_key(key)
    item = session.scalar(select(TgAuthorizationOnlineAbcItem).where(
        TgAuthorizationOnlineAbcItem.batch_id == batch_id,
        TgAuthorizationOnlineAbcItem.account_id == account_id,
    ))
    if not item:
        return None
    row = session.scalar(select(AuditLog).where(
        AuditLog.target_type == "tg_authorization_online_abc_items",
        AuditLog.target_id == item.id,
        AuditLog.action == MANUAL_ACTION,
        AuditLog.detail.contains(f"idempotency_key={normalized};"),
    ).order_by(AuditLog.id.desc()).limit(1))
    if not row:
        return None
    match = re.search(r"fingerprint=([0-9a-f]{64});", row.detail)
    if not match:
        raise AuthorizationDrError("online_abc_manual_outcome_audit_invalid", "Manual outcome audit is malformed")
    return _result(session, batch_id, account_id, fingerprint=match.group(1))


def _idempotent_result(existing: dict, expected_fingerprint: str) -> dict:
    if existing["fingerprint"] != expected_fingerprint:
        raise AuthorizationDrError("idempotency_key_conflict", "Manual outcome key was already used")
    return {**existing, "already_applied": True}


def _result(session, batch_id: str, account_id: int, *, fingerprint: str) -> dict:
    batch = _batch(session, batch_id)
    item = _item(session, batch, account_id)
    slots = _slots(session, item)
    return {
        "batch_id": batch.id,
        "batch_status": batch.status,
        "batch_version": batch.version,
        "execution_release_sha": batch.execution_release_sha,
        "account_id": item.account_id,
        "item_status": item.status,
        "item_outcome": item.outcome,
        "item_version": item.version,
        "b_outcome": slots["standby_1"].outcome,
        "c_outcome": slots["standby_2"].outcome,
        "fingerprint": fingerprint,
    }


def _approval(
    batch, *, requested_by: str, approved_by: str, approval_ref: str,
) -> tuple[str, str, str]:
    values = tuple(value.strip() for value in (requested_by, approved_by, approval_ref))
    valid = all(values) and values[0] != values[1]
    valid = valid and values[:2] == (batch.requested_by, batch.approved_by)
    if not valid:
        raise AuthorizationDrError("online_abc_runner_approval_mismatch", "Manual outcome approval is invalid")
    return values


def _release_sha(value: str) -> str:
    normalized = value.strip().lower()
    if not SHA_PATTERN.fullmatch(normalized):
        raise AuthorizationDrError("runtime_image_mismatch", "Current release SHA is unavailable")
    return normalized


def _idempotency_key(value: str) -> str:
    normalized = value.strip()
    if not KEY_PATTERN.fullmatch(normalized):
        raise AuthorizationDrError("idempotency_key_required", "Manual outcome idempotency key is invalid")
    return normalized


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


def _item(session, batch, account_id: int):
    item = session.scalar(select(TgAuthorizationOnlineAbcItem).where(
        TgAuthorizationOnlineAbcItem.batch_id == batch.id,
        TgAuthorizationOnlineAbcItem.account_id == account_id,
    ))
    if not item:
        raise AuthorizationDrError("online_abc_item_not_found", "Online ABC item is unavailable")
    return item


def _locked_item(session, batch_id: str, account_id: int):
    item = session.scalar(select(TgAuthorizationOnlineAbcItem).where(
        TgAuthorizationOnlineAbcItem.batch_id == batch_id,
        TgAuthorizationOnlineAbcItem.account_id == account_id,
    ).with_for_update().execution_options(populate_existing=True))
    if not item:
        raise AuthorizationDrError("online_abc_item_not_found", "Online ABC item is unavailable")
    return item


def _slots(session, item) -> dict:
    values = list(session.scalars(select(TgAuthorizationOnlineAbcSlotResult).where(
        TgAuthorizationOnlineAbcSlotResult.item_id == item.id,
    )))
    result = {value.logical_slot: value for value in values}
    if set(result) != {"standby_1", "standby_2"}:
        raise AuthorizationDrError("online_abc_slot_not_found", "Online ABC slots are incomplete")
    return result


def _fingerprint(payload: dict) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()


__all__ = [
    "apply_manual_online_abc_outcome",
    "preview_manual_online_abc_outcome",
    "read_manual_online_abc_outcome",
]

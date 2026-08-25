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
    TgAccountOnlineState,
    TgAuthorizationDrOperation,
    TgAuthorizationOnlineAbcBatch,
    TgAuthorizationOnlineAbcItem,
)
from app.security import decrypt_session
from app.services._common import _now, audit, gateway
from app.services.developer_apps import credentials_for_authorization

from .abc_verify import _standby_b, _standby_c
from .contracts import AuthorizationDrError
from .online_abc import UNKNOWN_OPERATION_STATUSES
from .online_abc_manifest import ACTIVE_OPERATION_STATUSES
from .online_abc_operations import online_abc_item_operations
from .online_abc_primary import primary_state


MANUAL_ACTION = "确认 ABC completed A/B 双失效人工债务并继续"
MANUAL_BLOCKER = "primary_and_sv_standby_unavailable"
PRIMARY_DRIFT_OUTCOME = "primary_drift_after_success"
B_FAILURE_CODE = "session_not_authorized"
KEY_PATTERN = re.compile(r"[A-Za-z0-9:._-]{1,100}")
SHA_PATTERN = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def preview_primary_failure_manual_outcome(
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
    item = _item(session, batch_id, account_id)
    approval = _approval(
        batch, requested_by=requested_by, approved_by=approved_by, approval_ref=approval_ref,
    )
    boundary = _batch_boundary(session, batch, item)
    inflight = _inflight_checkpoint(session, batch, item)
    facts = _failure_facts(session, item)
    _probe_failed_b(session, facts[2])
    global_state = _global_boundary(session)
    payload = _payload(
        batch=batch, item=item, facts=facts, boundary=boundary, global_state=global_state,
        inflight=inflight,
        release_sha=_release_sha(runtime_release_sha),
        key=_key(idempotency_key),
        approval=approval,
    )
    return {**payload, "fingerprint": _fingerprint(payload)}


def apply_primary_failure_manual_outcome(
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
    _lock(session, batch_id, account_id)
    preview = preview_primary_failure_manual_outcome(
        session, batch_id, account_id,
        runtime_release_sha=runtime_release_sha,
        idempotency_key=idempotency_key,
        requested_by=requested_by,
        approved_by=approved_by,
        approval_ref=approval_ref,
    )
    if preview["fingerprint"] != expected_fingerprint:
        raise AuthorizationDrError("migration_fingerprint_conflict", "Primary failure preview changed")
    _apply(session, preview)
    session.commit()
    result = _result(session, batch_id, account_id, fingerprint=preview["fingerprint"])
    return {**result, "already_applied": False}


def read_primary_failure_manual_outcome(
    session, batch_id: str, account_id: int, *, idempotency_key: str,
) -> dict:
    result = _existing(session, batch_id, account_id, key=idempotency_key)
    if not result:
        raise AuthorizationDrError("online_abc_primary_manual_not_found", "Primary manual audit is unavailable")
    return {**result, "already_applied": True}


def _failure_facts(session, item):
    account = session.get(TgAccount, item.account_id)
    primary = session.get(TgAccountAuthorization, item.primary_authorization_id)
    state = session.scalar(select(TgAccountOnlineState).where(
        TgAccountOnlineState.tenant_id == item.tenant_id,
        TgAccountOnlineState.account_id == item.account_id,
    ))
    _require_failed_primary(account, primary, state, item=item)
    standby_b = _standby_b(session, account, primary)
    standby_c, bundle, copy_count, probe = _standby_c(session, account, primary)
    completed = _completed_operations(session, item)
    return account, primary, standby_b, standby_c, bundle, copy_count, probe, state, completed


def _require_failed_primary(account, primary, state, *, item) -> None:
    fact_delta = account.authorization_fact_generation - item.authorization_fact_generation if account else -1
    primary_delta = primary.fact_version - item.primary_fact_version if primary else -1
    valid = bool(
        account and primary and state
        and account.status in {AccountStatus.SESSION_EXPIRED.value, AccountStatus.NEED_RELOGIN.value}
        and state.desired_online and state.online_status == "login_required" and state.last_probe_at
        and account.current_authorization_id == primary.id and primary.is_current and primary.is_slot_current
        and account.session_ciphertext == primary.session_ciphertext
        and _digest(primary.session_ciphertext or "") == item.primary_session_digest
        and account.authorization_generation == item.authorization_generation
        and account.connection_generation == item.connection_generation
        and fact_delta == primary_delta and fact_delta >= 1
        and primary.status == "active" and primary.health_status == "healthy"
        and primary.protected_from_cleanup and not primary.last_authoritative_error_code
    )
    if not valid:
        raise AuthorizationDrError("online_abc_primary_failure_unproven", "Completed A failure changed")


def _completed_operations(session, item) -> dict:
    operations = online_abc_item_operations(session, _batch(session, item.batch_id), item)
    valid = all(operations[name] and operations[name].status == "succeeded" for name in ("b", "c", "e4"))
    slots = _slots(session, item)
    slots_valid = (
        item.primary_probe_outcome == "succeeded"
        and slots["standby_1"].outcome == "succeeded"
        and slots["standby_1"].operation_id == operations["b"].id
        and slots["standby_2"].outcome == "succeeded"
        and slots["standby_2"].operation_id == operations["c"].id
    ) if valid else False
    remote_id = _e4_remote_id(session, operations["e4"]) if valid else ""
    if not valid or not slots_valid or not remote_id:
        raise AuthorizationDrError("online_abc_primary_manual_state_invalid", "Completed B/C/E4 evidence changed")
    return {
        "slots": [
            [slots[name].id, slots[name].version, slots[name].outcome, slots[name].operation_id]
            for name in ("standby_1", "standby_2")
        ],
        "operations": [_operation_snapshot(operations[name]) for name in ("b", "c", "e4")],
        "e4_remote_id": remote_id,
    }


def _e4_remote_id(session, operation) -> str:
    row = session.scalar(select(AuditLog).where(
        AuditLog.target_type == "tg_authorization_dr_operation",
        AuditLog.target_id == operation.id,
        AuditLog.action == "完成 ABC canary E4",
        AuditLog.detail.contains("primary_saved_message_id="),
    ).order_by(AuditLog.id.desc()).limit(1))
    if not row:
        return ""
    return row.detail.split("primary_saved_message_id=", 1)[1].split(";", 1)[0].strip()


def _probe_failed_b(session, standby_b) -> None:
    try:
        gateway.authorization_identity(
            decrypt_session(standby_b.session_ciphertext),
            credentials_for_authorization(session, standby_b),
        )
    except RuntimeError as exc:
        if str(exc) == "session is not authorized":
            return
        raise AuthorizationDrError("online_abc_standby_failure_unproven", type(exc).__name__) from exc
    except Exception as exc:
        raise AuthorizationDrError("online_abc_standby_failure_unproven", type(exc).__name__) from exc
    raise AuthorizationDrError("online_abc_standby_still_usable", "SV B remains authorized")


def _batch_boundary(session, batch, item) -> dict:
    counts = Counter(row.status for row in _items(session, batch.id))
    valid = (
        batch.selection_mode == "all_online_accounts" and batch.status == "stopped"
        and sum(counts.values()) == batch.target_count and counts["stopped"] == 1
        and counts["running"] <= 1 and counts["pending"] > 0
        and item.status == "stopped" and item.outcome == PRIMARY_DRIFT_OUTCOME
        and item.blocker_code == PRIMARY_DRIFT_OUTCOME
        and not set(counts) - {"pending", "succeeded", "manual_required", "running", "stopped"}
    )
    if not valid:
        raise AuthorizationDrError("online_abc_primary_manual_batch_invalid", "Full batch boundary changed")
    return dict(counts)


def _inflight_checkpoint(session, batch, manual_item) -> list:
    rows = [row for row in _items(session, batch.id) if row.status == "running"]
    if not rows:
        return []
    item = rows[0]
    if item.id == manual_item.id:
        raise AuthorizationDrError("online_abc_primary_manual_batch_invalid", "Manual item cannot be running")
    account = session.get(TgAccount, item.account_id)
    primary = session.get(TgAccountAuthorization, item.primary_authorization_id)
    operations = online_abc_item_operations(session, batch, item)
    completed = all(operations[name] and operations[name].status == "succeeded" for name in ("b", "c", "e4"))
    if primary_state(account, primary, item) != "qualified" or not completed:
        raise AuthorizationDrError("online_abc_primary_manual_inflight_invalid", "Running checkpoint changed")
    _standby_b(session, account, primary)
    _standby_c(session, account, primary)
    remote_id = _e4_remote_id(session, operations["e4"])
    if not remote_id:
        raise AuthorizationDrError("online_abc_primary_manual_inflight_invalid", "Running E4 evidence changed")
    return [
        item.id, item.account_id, item.version, primary.id, primary.fact_version,
        _digest(primary.session_ciphertext or ""),
        account.authorization_generation, account.authorization_fact_generation,
        account.connection_generation,
        *[_operation_snapshot(operations[name]) for name in ("b", "c", "e4")],
        remote_id,
    ]


def _global_boundary(session) -> dict:
    runtime = session.get(AuthorizationDrRuntimeContract, 1)
    unknown = _operation_count(session, UNKNOWN_OPERATION_STATUSES)
    sensitive = _operation_count(session, ACTIVE_OPERATION_STATUSES)
    my_clients = session.scalar(select(func.coalesce(func.sum(
        AuthorizationDrExecutionNode.active_client_count,
    ), 0)).where(AuthorizationDrExecutionNode.region_code == "my"))
    valid = runtime and runtime.mode == "off" and not runtime.claim_scope_operation_id
    if not valid or unknown or sensitive or my_clients:
        raise AuthorizationDrError("online_abc_primary_manual_runtime_active", "Global DR boundary is not quiescent")
    return {"runtime_mode": "off", "runtime_scope": "", "unknown": 0, "sensitive": 0, "my_clients": 0}


def _payload(
    *, batch, item, facts, boundary, global_state, inflight, release_sha, key, approval,
) -> dict:
    account, primary, standby_b, standby_c, bundle, copy_count, probe, state, completed = facts
    return {
        "batch_id": batch.id, "batch_version": batch.version,
        "account_id": account.id, "item_id": item.id, "item_version": item.version,
        "primary": _authorization_snapshot(account, primary),
        "primary_failure": [
            account.status, state.online_status, str(state.last_probe_at),
            _digest(state.failure_detail or ""),
        ],
        "standby_b": _standby_snapshot(standby_b), "standby_b_failure": B_FAILURE_CODE,
        "standby_c": _standby_snapshot(standby_c),
        "bundle": [bundle.id, bundle.bundle_generation, bundle.receipt_status, copy_count, probe.id, probe.status],
        "completed": completed, "counts": boundary,
        "inflight_checkpoint": inflight, "global": global_state,
        "previous_execution_release_sha": batch.execution_release_sha or batch.deployed_release_sha,
        "runtime_release_sha": release_sha, "idempotency_key": key,
        "requested_by": approval[0], "approved_by": approval[1], "approval_ref": approval[2],
    }


def _authorization_snapshot(account, primary) -> list:
    return [
        primary.id, primary.fact_version, primary.session_ciphertext and _digest(primary.session_ciphertext),
        account.authorization_generation, account.authorization_fact_generation, account.connection_generation,
    ]


def _standby_snapshot(authorization) -> list:
    return [
        authorization.id, authorization.fact_version, authorization.status, authorization.health_status,
        authorization.logical_slot, authorization.slot_generation,
        authorization.auth_key_fingerprint_digest, authorization.telegram_user_id_digest,
    ]


def _operation_snapshot(operation) -> list:
    return [
        operation.id, operation.operation_version, operation.status,
        operation.remote_call_state, operation.reconcile_status,
        operation.candidate_authorization_id,
    ]


def _apply(session, preview: dict) -> None:
    batch = _batch(session, preview["batch_id"])
    item = _item(session, batch.id, preview["account_id"])
    standby_b = session.get(TgAccountAuthorization, preview["standby_b"][0])
    slots = _slots(session, item)
    _project_failed_b(standby_b)
    slots["standby_1"].outcome = "manual_required"
    slots["standby_1"].blocker_code = MANUAL_BLOCKER
    slots["standby_1"].version += 1
    item.status = item.outcome = "manual_required"
    item.blocker_code = MANUAL_BLOCKER
    item.finished_at = _now()
    item.version += 1
    batch.execution_release_sha = preview["runtime_release_sha"]
    batch.status = "running"
    batch.version += 1
    _audit(session, batch, item, preview=preview)


def _project_failed_b(standby_b) -> None:
    standby_b.status = "invalid"
    standby_b.health_status = "invalid"
    standby_b.dr_state = "needs_repair"
    standby_b.remote_authorization_state = "invalid"
    standby_b.failure_reason = B_FAILURE_CODE
    standby_b.last_authoritative_error_code = B_FAILURE_CODE
    standby_b.last_authoritative_observed_at = _now()
    standby_b.fact_version += 1


def _audit(session, batch, item, *, preview) -> None:
    audit(
        session, tenant_id=batch.tenant_id, actor=preview["approved_by"], action=MANUAL_ACTION,
        target_type="tg_authorization_online_abc_items", target_id=item.id,
        detail=(
            f"account_id={item.account_id}; approval_ref={preview['approval_ref']}; "
            f"idempotency_key={preview['idempotency_key']}; fingerprint={preview['fingerprint']}; "
            f"blocker={MANUAL_BLOCKER}; b_failure={B_FAILURE_CODE}; "
            f"batch_version={preview['batch_version']}->{batch.version}; "
            f"item_version={preview['item_version']}->{item.version}; "
            f"execution_release={preview['previous_execution_release_sha']}->{preview['runtime_release_sha']}"
        ),
    )


def _existing(session, batch_id: str, account_id: int, *, key: str) -> dict | None:
    item = _item_or_none(session, batch_id, account_id)
    if not item:
        return None
    row = session.scalar(select(AuditLog).where(
        AuditLog.target_type == "tg_authorization_online_abc_items",
        AuditLog.target_id == item.id,
        AuditLog.action == MANUAL_ACTION,
        AuditLog.detail.contains(f"idempotency_key={_key(key)};"),
    ).order_by(AuditLog.id.desc()).limit(1))
    match = re.search(r"fingerprint=([0-9a-f]{64});", row.detail) if row else None
    return _result(session, batch_id, account_id, fingerprint=match.group(1)) if match else None


def _result(session, batch_id: str, account_id: int, *, fingerprint: str) -> dict:
    batch = _batch(session, batch_id)
    item = _item(session, batch_id, account_id)
    slots = _slots(session, item)
    return {
        "batch_id": batch.id, "batch_status": batch.status, "batch_version": batch.version,
        "execution_release_sha": batch.execution_release_sha, "account_id": item.account_id,
        "item_status": item.status, "item_outcome": item.outcome,
        "item_blocker_code": item.blocker_code, "item_version": item.version,
        "b_outcome": slots["standby_1"].outcome, "c_outcome": slots["standby_2"].outcome,
        "fingerprint": fingerprint,
    }


def _idempotent(existing: dict, expected: str) -> dict:
    if existing["fingerprint"] != expected:
        raise AuthorizationDrError("idempotency_key_conflict", "Primary manual key was already used")
    return {**existing, "already_applied": True}


def _approval(
    batch, *, requested_by: str, approved_by: str, approval_ref: str,
) -> tuple[str, str, str]:
    values = tuple(value.strip() for value in (requested_by, approved_by, approval_ref))
    if not all(values) or values[0] == values[1] or values[:2] != (batch.requested_by, batch.approved_by):
        raise AuthorizationDrError("online_abc_runner_approval_mismatch", "Primary manual approval is invalid")
    return values


def _release_sha(value: str) -> str:
    normalized = value.strip().lower()
    if not SHA_PATTERN.fullmatch(normalized):
        raise AuthorizationDrError("runtime_image_mismatch", "Current release SHA is unavailable")
    return normalized


def _key(value: str) -> str:
    normalized = value.strip()
    if not KEY_PATTERN.fullmatch(normalized):
        raise AuthorizationDrError("idempotency_key_required", "Primary manual key is invalid")
    return normalized


def _operation_count(session, statuses) -> int:
    return int(session.scalar(select(func.count()).select_from(TgAuthorizationDrOperation).where(
        TgAuthorizationDrOperation.status.in_(statuses),
    )) or 0)


def _batch(session, batch_id: str):
    row = session.get(TgAuthorizationOnlineAbcBatch, batch_id)
    if not row:
        raise AuthorizationDrError("online_abc_batch_not_found", "Online ABC batch is unavailable")
    return row


def _item(session, batch_id: str, account_id: int):
    row = _item_or_none(session, batch_id, account_id)
    if not row:
        raise AuthorizationDrError("online_abc_item_not_found", "Online ABC item is unavailable")
    return row


def _item_or_none(session, batch_id: str, account_id: int):
    return session.scalar(select(TgAuthorizationOnlineAbcItem).where(
        TgAuthorizationOnlineAbcItem.batch_id == batch_id,
        TgAuthorizationOnlineAbcItem.account_id == account_id,
    ))


def _items(session, batch_id: str):
    return list(session.scalars(select(TgAuthorizationOnlineAbcItem).where(
        TgAuthorizationOnlineAbcItem.batch_id == batch_id,
    )))


def _slots(session, item) -> dict:
    from app.models import TgAuthorizationOnlineAbcSlotResult

    rows = list(session.scalars(select(TgAuthorizationOnlineAbcSlotResult).where(
        TgAuthorizationOnlineAbcSlotResult.item_id == item.id,
    )))
    result = {row.logical_slot: row for row in rows}
    if set(result) != {"standby_1", "standby_2"}:
        raise AuthorizationDrError("online_abc_slot_not_found", "Online ABC slots are incomplete")
    return result


def _lock(session, batch_id: str, account_id: int) -> None:
    session.scalar(select(TgAuthorizationOnlineAbcBatch).where(
        TgAuthorizationOnlineAbcBatch.id == batch_id,
    ).with_for_update().execution_options(populate_existing=True))
    session.scalar(select(TgAuthorizationOnlineAbcItem).where(
        TgAuthorizationOnlineAbcItem.batch_id == batch_id,
        TgAuthorizationOnlineAbcItem.account_id == account_id,
    ).with_for_update().execution_options(populate_existing=True))


def _fingerprint(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


__all__ = [
    "apply_primary_failure_manual_outcome",
    "preview_primary_failure_manual_outcome",
    "read_primary_failure_manual_outcome",
]

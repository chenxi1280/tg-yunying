from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from typing import Callable

from sqlalchemy import select

from app.models import (
    AuthorizationDrRuntimeContract,
    TgAccount,
    TgAccountAuthorization,
    TgAuthorizationDrOperation,
    TgAuthorizationOnlineAbcBatch,
    TgAuthorizationOnlineAbcItem,
)
from app.services._common import _now, audit
from app.services.authorization_canonical_backfill import (
    preview_primary_qualification,
    qualify_primary_authorization,
)

from .abc_backup import apply_abc_backup, preview_abc_backup
from .abc_canary import prepare_scoped_c_migration
from .abc_verify import apply_abc_e4, preview_abc_e4
from .contracts import AuthorizationDrError
from .online_abc import (
    online_abc_batch_status,
    start_next_online_abc_item,
    sync_online_abc_batch,
)
from .online_abc_operations import online_abc_item_operations, online_abc_operation_keys
from .readiness import ready_migration_runtime_image_sha


POLL_INTERVAL_SECONDS = 2.0
SUCCESS_STATUS = "succeeded"
TERMINAL_BATCH_STATUSES = {"observing", "stopped"}
RESUMABLE_RUNNER_BLOCKERS = {"malaysia_wake_unavailable"}
RETRYABLE_E4_READINESS_CODES = {"malaysia_wake_unavailable"}
TERMINAL_OPERATION_STATUSES = {
    SUCCESS_STATUS,
    "failed",
    "manual_required",
    "migration_rolled_back_forward",
    "reconcile_unknown",
}


@dataclass(frozen=True)
class RunnerApproval:
    requested_by: str
    approved_by: str
    approval_ref: str


def online_abc_runner_status(session, batch_id: str) -> dict:
    batch = _batch(session, batch_id)
    item = _current_item(session, batch.id)
    operations = online_abc_item_operations(session, batch, item) if item else _empty_operations()
    contract = session.get(AuthorizationDrRuntimeContract, 1)
    return {
        "batch": online_abc_batch_status(session, batch.id),
        "current_item": _item_out(item),
        "operations": {name: _operation_out(value) for name, value in operations.items()},
        "runtime": _runtime_out(contract),
        "next_action": _next_action(batch.status, item, operations),
        "terminal_reason": _terminal_reason(batch.status, item, operations),
    }


def run_online_abc_batch(
    session,
    batch_id: str,
    *,
    requested_by: str,
    approved_by: str,
    approval_ref: str,
    runtime_release_sha: str,
    poll_seconds: float = POLL_INTERVAL_SECONDS,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict:
    approval = _approval(requested_by, approved_by, approval_ref)
    batch = _batch(session, batch_id)
    _require_batch_contract(batch, approval, runtime_release_sha)
    if poll_seconds <= 0:
        raise AuthorizationDrError("poll_interval_invalid", "Runner poll interval must be positive")
    while True:
        view = online_abc_runner_status(session, batch_id)
        if view["batch"]["status"] in TERMINAL_BATCH_STATUSES:
            return view
        command = start_next_online_abc_item(
            session, batch_id, actor=approval.approved_by, approval_ref=approval.approval_ref,
        )
        try:
            _run_current_item(session, batch_id, command, approval, poll_seconds, sleeper)
        except Exception as exc:
            return _stop_after_error(session, batch_id, approval, exc)
        sync_online_abc_batch(
            session, batch_id, actor=approval.approved_by, approval_ref=approval.approval_ref,
        )


def resume_online_abc_batch(
    session,
    batch_id: str,
    *,
    requested_by: str,
    approved_by: str,
    approval_ref: str,
    runtime_release_sha: str,
) -> dict:
    approval = _approval(requested_by, approved_by, approval_ref)
    batch = _locked_batch(session, batch_id)
    _require_batch_contract(batch, approval, runtime_release_sha)
    item = _resumable_item(session, batch)
    operations = online_abc_item_operations(session, batch, item)
    _require_resume_contract(session, item, operations)
    ready_migration_runtime_image_sha(session)
    _resume_item(session, batch, item, approval)
    session.commit()
    return online_abc_runner_status(session, batch_id)


def _run_current_item(session, batch_id, command, approval, poll_seconds, sleeper) -> None:
    batch, item, operations = _context(session, batch_id)
    if item.id != command["item_id"] or item.account_id != command["account_id"]:
        raise AuthorizationDrError("online_abc_runner_item_drift", "Started ABC item changed")
    if operations["b"] is None:
        _ensure_primary_qualified(session, item, approval)
        _create_b_and_c(session, batch, item, approval)
    batch, item, operations = _context(session, batch_id)
    _require_succeeded(operations["b"], "online_abc_runner_b_incomplete")
    if operations["c"] is None:
        _create_c(session, batch, item, approval)
    _wait_for_c(session, batch_id, poll_seconds, sleeper)
    batch, item, operations = _context(session, batch_id)
    _require_succeeded(operations["c"], "online_abc_runner_c_incomplete")
    if operations["e4"] is None:
        _create_e4(session, batch, item, approval, poll_seconds, sleeper)
    _require_succeeded(_context(session, batch_id)[2]["e4"], "online_abc_runner_e4_incomplete")


def _ensure_primary_qualified(session, item, approval: RunnerApproval) -> None:
    account = session.get(TgAccount, item.account_id)
    primary = session.get(TgAccountAuthorization, item.primary_authorization_id)
    state = _primary_state(account, primary, item)
    if state == "qualified":
        return
    if state != "frozen":
        raise AuthorizationDrError("online_abc_primary_drift", "Frozen A changed before qualification")
    preview = preview_primary_qualification(session, item.tenant_id, item.account_id)
    qualify_primary_authorization(
        session,
        item.tenant_id,
        item.account_id,
        expected_fingerprint=preview["fingerprint"],
        actor=approval.approved_by,
        approval_ref=approval.approval_ref,
    )
    session.expire_all()
    account = session.get(TgAccount, item.account_id)
    primary = session.get(TgAccountAuthorization, item.primary_authorization_id)
    if _primary_state(account, primary, item) != "qualified":
        raise AuthorizationDrError("online_abc_primary_drift", "A qualification did not preserve frozen facts")


def _create_b_and_c(session, batch, item, approval: RunnerApproval) -> None:
    keys = online_abc_operation_keys(batch, item)
    preview = preview_abc_backup(
        session, batch.tenant_id, item.account_id, idempotency_key=keys["b"],
    )
    apply_abc_backup(
        session,
        batch.tenant_id,
        item.account_id,
        idempotency_key=keys["b"],
        expected_fingerprint=preview["fingerprint"],
        requested_by=approval.requested_by,
        approved_by=approval.approved_by,
        approval_ref=approval.approval_ref,
    )
    _create_c(session, batch, item, approval)


def _create_c(session, batch, item, approval: RunnerApproval) -> None:
    keys = online_abc_operation_keys(batch, item)
    runtime_sha = ready_migration_runtime_image_sha(session)
    prepare_scoped_c_migration(
        session,
        batch.tenant_id,
        item.account_id,
        idempotency_key=keys["c"],
        requested_by=approval.requested_by,
        approved_by=approval.approved_by,
        approval_ref=approval.approval_ref,
        runtime_image_sha=runtime_sha,
    )


def _create_e4(session, batch, item, approval: RunnerApproval, poll_seconds: float, sleeper) -> None:
    key = online_abc_operation_keys(batch, item)["e4"]
    preview = _wait_for_e4_preview(session, batch, item, key, poll_seconds, sleeper)
    apply_abc_e4(
        session,
        batch.tenant_id,
        item.account_id,
        idempotency_key=key,
        expected_fingerprint=preview["fingerprint"],
        requested_by=approval.requested_by,
        approved_by=approval.approved_by,
        approval_ref=approval.approval_ref,
    )


def _wait_for_e4_preview(session, batch, item, key: str, poll_seconds: float, sleeper) -> dict:
    while True:
        try:
            return preview_abc_e4(session, batch.tenant_id, item.account_id, idempotency_key=key)
        except AuthorizationDrError as exc:
            if exc.code not in RETRYABLE_E4_READINESS_CODES:
                raise
            sleeper(poll_seconds)
            session.expire_all()


def _wait_for_c(session, batch_id: str, poll_seconds: float, sleeper) -> None:
    while True:
        operation = _context(session, batch_id)[2]["c"]
        if operation is None:
            raise AuthorizationDrError("migration_operation_not_found", "C operation disappeared")
        if operation.status in TERMINAL_OPERATION_STATUSES:
            return
        sleeper(poll_seconds)
        session.expire_all()


def _stop_after_error(session, batch_id: str, approval: RunnerApproval, exc: Exception) -> dict:
    session.rollback()
    synced = sync_online_abc_batch(
        session, batch_id, actor=approval.approved_by, approval_ref=approval.approval_ref,
    )
    if synced["status"] != "stopped":
        code = exc.code if isinstance(exc, AuthorizationDrError) else type(exc).__name__
        _stop_running_item(
            session,
            batch_id,
            blocker_code=code,
            actor=approval.approved_by,
            approval_ref=approval.approval_ref,
        )
    return online_abc_runner_status(session, batch_id)


def _stop_running_item(session, batch_id: str, *, blocker_code: str, actor: str, approval_ref: str) -> None:
    batch = session.scalar(select(TgAuthorizationOnlineAbcBatch).where(
        TgAuthorizationOnlineAbcBatch.id == batch_id,
    ).with_for_update())
    item = _running_item(session, batch.id) if batch else None
    if not batch or not item:
        return
    item.status = "stopped"
    item.outcome = "runner_blocked"
    item.blocker_code = blocker_code[:100]
    item.finished_at = _now()
    item.version += 1
    batch.status = "stopped"
    batch.version += 1
    audit(
        session,
        tenant_id=batch.tenant_id,
        actor=actor,
        action=f"runner 停止 ABC account={item.account_id}",
        target_type="tg_authorization_online_abc_batches",
        target_id=batch.id,
        detail=f"approval_ref={approval_ref}; blocker_code={item.blocker_code}",
    )
    session.commit()


def _resumable_item(session, batch) -> TgAuthorizationOnlineAbcItem:
    if batch.status != "stopped":
        raise AuthorizationDrError("online_abc_resume_not_stopped", f"Batch is {batch.status}")
    items = list(session.scalars(select(TgAuthorizationOnlineAbcItem).where(
        TgAuthorizationOnlineAbcItem.batch_id == batch.id,
        TgAuthorizationOnlineAbcItem.status == "stopped",
        TgAuthorizationOnlineAbcItem.outcome == "runner_blocked",
    ).with_for_update()))
    if len(items) != 1:
        raise AuthorizationDrError("online_abc_resume_ambiguous", "Exactly one runner-blocked item is required")
    return items[0]


def _require_resume_contract(session, item, operations: dict) -> None:
    if item.blocker_code not in RESUMABLE_RUNNER_BLOCKERS:
        raise AuthorizationDrError("online_abc_resume_blocker_forbidden", f"Blocker is {item.blocker_code}")
    _require_succeeded(operations["b"], "online_abc_runner_b_incomplete")
    _require_succeeded(operations["c"], "online_abc_runner_c_incomplete")
    if operations["e4"] is not None:
        raise AuthorizationDrError("online_abc_resume_remote_effect_started", "E4 operation already exists")
    unknown = session.scalar(select(TgAuthorizationDrOperation.id).where(
        TgAuthorizationDrOperation.status == "reconcile_unknown",
    ).limit(1))
    if unknown:
        raise AuthorizationDrError("global_reconcile_unknown", "Global reconcile unknown must be zero")
    account = session.get(TgAccount, item.account_id)
    primary = session.get(TgAccountAuthorization, item.primary_authorization_id)
    if _primary_state(account, primary, item) != "qualified":
        raise AuthorizationDrError("online_abc_primary_drift", "A changed before runner resume")


def _resume_item(session, batch, item, approval: RunnerApproval) -> None:
    item.status = "running"
    item.outcome = "running"
    item.blocker_code = ""
    item.finished_at = None
    item.version += 1
    batch.status = "running"
    batch.version += 1
    audit(
        session,
        tenant_id=batch.tenant_id,
        actor=approval.approved_by,
        action=f"恢复 ABC runner account={item.account_id}",
        target_type="tg_authorization_online_abc_batches",
        target_id=batch.id,
        detail=f"approval_ref={approval.approval_ref}; checkpoint=post_c_pre_e4",
    )


def _context(session, batch_id: str):
    session.expire_all()
    batch = _batch(session, batch_id)
    item = _running_item(session, batch.id)
    if not item:
        raise AuthorizationDrError("online_abc_runner_item_missing", "Running ABC item is unavailable")
    return batch, item, online_abc_item_operations(session, batch, item)


def _primary_state(account, primary, item) -> str:
    if not account or not primary or account.current_authorization_id != primary.id:
        return "drifted"
    frozen = _primary_dimensions(account, primary, item, fact_offset=0)
    qualified = _primary_dimensions(account, primary, item, fact_offset=1)
    if frozen:
        return "frozen"
    if qualified and primary.telegram_user_id_digest and primary.auth_key_fingerprint_digest:
        return "qualified"
    return "drifted"


def _primary_dimensions(account, primary, item, *, fact_offset: int) -> bool:
    return (
        account.authorization_generation == item.authorization_generation
        and account.authorization_fact_generation == item.authorization_fact_generation + fact_offset
        and account.connection_generation == item.connection_generation
        and primary.fact_version == item.primary_fact_version + fact_offset
        and hashlib.sha256((primary.session_ciphertext or "").encode()).hexdigest() == item.primary_session_digest
        and primary.is_current
    )


def _approval(requested_by: str, approved_by: str, approval_ref: str) -> RunnerApproval:
    values = [requested_by.strip(), approved_by.strip(), approval_ref.strip()]
    if not all(values):
        raise AuthorizationDrError("approval_ref_required", "Runner approval is incomplete")
    if values[0] == values[1]:
        raise AuthorizationDrError("approval_actor_conflict", "Runner approver must differ from requester")
    return RunnerApproval(*values)


def _require_batch_contract(batch, approval: RunnerApproval, runtime_release_sha: str) -> None:
    expected_approval = (batch.requested_by, batch.approved_by, batch.approval_ref)
    if expected_approval != (approval.requested_by, approval.approved_by, approval.approval_ref):
        raise AuthorizationDrError("online_abc_runner_approval_mismatch", "Runner approval differs from batch")
    release_sha = runtime_release_sha.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", release_sha):
        raise AuthorizationDrError("runtime_image_mismatch", "Current release SHA is unavailable")
    if batch.deployed_release_sha != release_sha:
        raise AuthorizationDrError("runtime_image_mismatch", "Batch release differs from current runtime")


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


def _current_item(session, batch_id: str):
    running = _running_item(session, batch_id)
    if running:
        return running
    return session.scalar(select(TgAuthorizationOnlineAbcItem).where(
        TgAuthorizationOnlineAbcItem.batch_id == batch_id,
        TgAuthorizationOnlineAbcItem.status == "pending",
    ).order_by(TgAuthorizationOnlineAbcItem.ordinal).limit(1))


def _running_item(session, batch_id: str):
    return session.scalar(select(TgAuthorizationOnlineAbcItem).where(
        TgAuthorizationOnlineAbcItem.batch_id == batch_id,
        TgAuthorizationOnlineAbcItem.status == "running",
    ).limit(1))


def _next_action(batch_status: str, item, operations: dict) -> str:
    if batch_status in TERMINAL_BATCH_STATUSES:
        return batch_status
    if item is None:
        return "start_item"
    for name in ("b", "c", "e4"):
        operation = operations[name]
        if operation is None:
            return {"b": "qualify_a_and_create_b", "c": "create_c", "e4": "verify_e4"}[name]
        if operation.status != SUCCESS_STATUS:
            return f"wait_or_stop_{name}:{operation.status}"
    return "sync_item"


def _terminal_reason(batch_status: str, item, operations: dict) -> str:
    if batch_status == "observing":
        return "batch_observing"
    if batch_status == "stopped" and item and item.blocker_code:
        return item.blocker_code
    for operation in operations.values():
        if operation and operation.status in TERMINAL_OPERATION_STATUSES - {SUCCESS_STATUS}:
            return operation.blocker_code or operation.status
    return ""


def _require_succeeded(operation, code: str) -> None:
    if operation is None or operation.status != SUCCESS_STATUS:
        status = operation.status if operation else "missing"
        raise AuthorizationDrError(code, f"Operation is {status}")


def _item_out(item) -> dict | None:
    if not item:
        return None
    return {"id": item.id, "ordinal": item.ordinal, "account_id": item.account_id, "status": item.status}


def _operation_out(operation) -> dict | None:
    if not operation:
        return None
    return {
        "id": operation.id,
        "status": operation.status,
        "blocker_code": operation.blocker_code,
        "candidate_authorization_id": operation.candidate_authorization_id,
    }


def _runtime_out(contract) -> dict:
    if not contract:
        return {"mode": "missing", "claim_scope_operation_id": ""}
    return {"mode": contract.mode, "claim_scope_operation_id": contract.claim_scope_operation_id}


def _empty_operations() -> dict:
    return {"b": None, "c": None, "e4": None}


__all__ = ["online_abc_runner_status", "resume_online_abc_batch", "run_online_abc_batch"]

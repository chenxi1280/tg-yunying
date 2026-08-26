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
    TgAuthorizationDrOperation,
    TgAuthorizationOnlineAbcBatch,
    TgAuthorizationOnlineAbcItem,
)
from app.services._common import audit

from .contracts import AuthorizationDrError
from .online_abc import UNKNOWN_OPERATION_STATUSES, online_abc_batch_status
from .online_abc_exception_queue import (
    apply_online_abc_exception,
    apply_previewed_online_abc_exception,
    list_online_abc_exceptions,
    preview_online_abc_exception,
)
from .online_abc_exception_state import require_exception_primaries_unchanged
from .online_abc_manifest import ACTIVE_OPERATION_STATUSES
from .online_abc_runner import run_next_online_abc_item


START_ACTION = "启动 ABC frozen-N one-shot sweep"
CHECKPOINT_ACTION = "ABC one-shot sweep checkpoint"
PAUSE_ACTION = "暂停 ABC one-shot sweep"
DEFAULT_CHECKPOINT_INTERVAL = 30
KEY_PATTERN = re.compile(r"[A-Za-z0-9:._-]{1,100}")
SHA_PATTERN = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
TERMINAL_ITEM_STATUSES = {"deferred_reconcile", "manual_required", "succeeded"}
TERMINAL_OPERATION_STATUSES = {
    "deferred_reconcile", "failed", "manual_required",
    "migration_rolled_back_forward", "succeeded",
}


def preview_online_abc_sweep_start(
    session,
    batch_id: str,
    *,
    runtime_release_sha: str,
    idempotency_key: str,
    requested_by: str,
    approved_by: str,
    approval_ref: str,
) -> dict:
    batch = _batch(session, batch_id)
    _require_startable(batch)
    approval = _approval(
        batch, requested_by=requested_by,
        approved_by=approved_by, approval_ref=approval_ref,
    )
    release_sha = _release_sha(runtime_release_sha)
    release_binding = _require_execution_release(session, batch, release_sha)
    key = _key(idempotency_key)
    current = _current_item(session, batch.id)
    exception = _current_exception_preview(
        session, batch, current, release_sha=release_binding["exception_release_sha"],
        key=key, approval=approval,
    )
    if not current:
        _require_global_quiescent(session)
    counts = _item_counts(session, batch.id)
    payload = {
        "batch_id": batch.id, "batch_version": batch.version, "batch_status": batch.status,
        "target_count": batch.target_count, "item_counts": dict(counts),
        "previous_execution_release_sha": batch.execution_release_sha or batch.deployed_release_sha,
        "runtime_release_sha": release_sha, "current_account_id": current.account_id if current else 0,
        "current_exception": exception, "idempotency_key": key,
        "execution_release_rebind": release_binding,
        "requested_by": approval[0], "approved_by": approval[1], "approval_ref": approval[2],
        "until_exhausted": True, "checkpoint_interval": DEFAULT_CHECKPOINT_INTERVAL,
    }
    return {**payload, "fingerprint": _fingerprint(payload)}


def apply_online_abc_sweep_start(
    session,
    batch_id: str,
    *,
    runtime_release_sha: str,
    idempotency_key: str,
    expected_fingerprint: str,
    requested_by: str,
    approved_by: str,
    approval_ref: str,
) -> dict:
    existing = _existing_start(session, batch_id, idempotency_key)
    if existing:
        return _idempotent(existing, expected_fingerprint)
    _lock_batch(session, batch_id)
    preview = preview_online_abc_sweep_start(
        session, batch_id, runtime_release_sha=runtime_release_sha,
        idempotency_key=idempotency_key, requested_by=requested_by,
        approved_by=approved_by, approval_ref=approval_ref,
    )
    if preview["fingerprint"] != expected_fingerprint:
        raise AuthorizationDrError("migration_fingerprint_conflict", "Sweep start preview changed")
    if preview["current_exception"]:
        exception = preview["current_exception"]
        apply_previewed_online_abc_exception(session, exception)
    batch = _lock_batch(session, batch_id)
    batch.execution_release_sha = preview["runtime_release_sha"]
    if _item_counts(session, batch.id)["pending"]:
        batch.status = "sweeping"
    else:
        _finish_batch(session, batch)
    batch.version += 1
    _audit_start(session, batch, preview)
    session.commit()
    return {**online_abc_sweep_status(session, batch.id), "already_applied": False}


def readback_online_abc_sweep_start(session, batch_id: str, *, idempotency_key: str) -> dict:
    existing = _existing_start(session, batch_id, idempotency_key)
    if not existing:
        raise AuthorizationDrError("online_abc_sweep_start_not_found", "Sweep start audit is unavailable")
    return {**existing, "already_applied": True}


def online_abc_sweep_status(session, batch_id: str) -> dict:
    batch_status = online_abc_batch_status(session, batch_id)
    exceptions = list_online_abc_exceptions(session, batch_id)
    counts = Counter(batch_status["account_outcome_counts"])
    return {
        "batch": batch_status,
        "sweep": {
            "active": batch_status["status"] == "sweeping",
            "pending_count": counts["pending"],
            "succeeded_count": counts["succeeded"],
            "manual_required_count": counts["manual_required"],
            "deferred_reconcile_count": counts["deferred_reconcile"],
            "terminal_count": sum(counts[name] for name in TERMINAL_ITEM_STATUSES),
            "checkpoint_interval": DEFAULT_CHECKPOINT_INTERVAL,
        },
        "exceptions": exceptions,
    }


def run_online_abc_sweep_once(session, *, runtime_release_sha: str, poll_seconds: float = 2.0) -> dict:
    if poll_seconds <= 0:
        raise AuthorizationDrError("poll_interval_invalid", "Sweep poll interval must be positive")
    batch = _sweeping_batch(session)
    if not batch:
        return {"status": "idle"}
    current = _current_item(session, batch.id)
    if current:
        return _collect_current(session, batch, current, runtime_release_sha=runtime_release_sha)
    counts = _item_counts(session, batch.id)
    if not counts["pending"]:
        _finish_batch(session, batch)
        batch.version += 1
        session.commit()
        return online_abc_sweep_status(session, batch.id)
    try:
        require_exception_primaries_unchanged(session, batch.id)
    except Exception as exc:
        session.rollback()
        return _pause(session, batch.id, _error_code(exc))
    try:
        result = run_next_online_abc_item(
            session, batch.id, requested_by=batch.requested_by, approved_by=batch.approved_by,
            approval_ref=batch.approval_ref, runtime_release_sha=runtime_release_sha,
            poll_seconds=poll_seconds,
        )
    except Exception as exc:
        session.rollback()
        return _recover_or_pause(session, batch.id, exc, runtime_release_sha=runtime_release_sha)
    if not result["item_terminal"]:
        current = _current_item(session, batch.id)
        if not current:
            return _pause(session, batch.id, "sweep_item_missing_after_error")
        return _collect_current(session, _batch(session, batch.id), current, runtime_release_sha=runtime_release_sha)
    return _checkpoint_or_status(session, batch.id)


def _collect_current(session, batch, item, *, runtime_release_sha: str) -> dict:
    key = f"abc-sweep:{batch.id}:{item.id}:v{item.version}"
    try:
        preview = preview_online_abc_exception(
            session, batch.id, item.account_id, runtime_release_sha=runtime_release_sha,
            idempotency_key=key, requested_by=batch.requested_by,
            approved_by=batch.approved_by, approval_ref=batch.approval_ref,
        )
        apply_online_abc_exception(
            session, batch.id, item.account_id, runtime_release_sha=runtime_release_sha,
            idempotency_key=key, expected_fingerprint=preview["fingerprint"],
            requested_by=batch.requested_by, approved_by=batch.approved_by,
            approval_ref=batch.approval_ref,
        )
    except Exception as exc:
        session.rollback()
        return _pause(session, batch.id, _error_code(exc))
    return _checkpoint_or_status(session, batch.id)


def _recover_or_pause(session, batch_id: str, exc: Exception, *, runtime_release_sha: str) -> dict:
    batch = _batch(session, batch_id)
    current = _current_item(session, batch.id)
    if current:
        return _collect_current(session, batch, current, runtime_release_sha=runtime_release_sha)
    return _pause(session, batch.id, _error_code(exc))


def _checkpoint_if_due(session, batch_id: str) -> None:
    batch = _lock_batch(session, batch_id)
    counts = _item_counts(session, batch.id)
    terminal = sum(counts[name] for name in TERMINAL_ITEM_STATUSES)
    previous = _last_checkpoint_total(session, batch.id)
    processed = terminal - previous
    final = not counts["pending"] and not counts["running"] and not counts["stopped"]
    if processed < DEFAULT_CHECKPOINT_INTERVAL and not final:
        return
    _require_global_quiescent(session)
    require_exception_primaries_unchanged(session, batch.id)
    batch.version += 1
    audit(
        session, tenant_id=batch.tenant_id, actor=batch.approved_by,
        action=CHECKPOINT_ACTION, target_type="tg_authorization_online_abc_batches",
        target_id=batch.id,
        detail=(
            f"approval_ref={batch.approval_ref}; terminal_total={terminal}; "
            f"processed_count={processed}; "
            f"succeeded={counts['succeeded']}; manual_required={counts['manual_required']}; "
            f"deferred_reconcile={counts['deferred_reconcile']}; pending={counts['pending']}; "
            f"execution_release={batch.execution_release_sha}; final={str(final).lower()}"
        ),
    )
    if final:
        _finish_batch(session, batch)
    session.commit()


def _checkpoint_or_status(session, batch_id: str) -> dict:
    try:
        _checkpoint_if_due(session, batch_id)
    except Exception as exc:
        session.rollback()
        return _pause(session, batch_id, _error_code(exc))
    return online_abc_sweep_status(session, batch_id)


def _pause(session, batch_id: str, blocker: str) -> dict:
    batch = _lock_batch(session, batch_id)
    batch.status = "sweep_paused"
    batch.version += 1
    audit(
        session, tenant_id=batch.tenant_id, actor=batch.approved_by,
        action=PAUSE_ACTION, target_type="tg_authorization_online_abc_batches",
        target_id=batch.id,
        detail=f"approval_ref={batch.approval_ref}; blocker={blocker[:100]}; no_further_rpc=true",
    )
    session.commit()
    return online_abc_sweep_status(session, batch.id)


def pause_online_abc_sweep_for_error(session, blocker: str) -> dict:
    batch = _sweeping_batch(session)
    if not batch:
        return {"status": "idle"}
    return _pause(session, batch.id, blocker)


def _finish_batch(session, batch) -> None:
    counts = _item_counts(session, batch.id)
    if counts["pending"] or counts["running"] or counts["stopped"]:
        raise AuthorizationDrError("online_abc_sweep_not_exhausted", "Sweep still has unfinished items")
    has_exceptions = bool(counts["manual_required"] or counts["deferred_reconcile"])
    batch.status = "completed_with_exceptions" if has_exceptions else "completed"


def _require_execution_release(session, batch, release_sha: str) -> dict:
    expected = batch.execution_release_sha or batch.deployed_release_sha
    if expected == release_sha:
        return {
            "required": False, "previous_release_sha": expected,
            "runtime_release_sha": release_sha, "exception_release_sha": release_sha,
        }
    allowed = batch.selection_mode == "all_online_accounts" and batch.status == "stopped"
    started = session.scalar(select(AuditLog.id).where(
        AuditLog.target_type == "tg_authorization_online_abc_batches",
        AuditLog.target_id == batch.id, AuditLog.action == START_ACTION,
    ).limit(1))
    if not allowed or started:
        raise AuthorizationDrError(
            "online_abc_sweep_release_rebind_required",
            "Batch execution release must be rebound before sweep start",
        )
    return {
        "required": True, "previous_release_sha": expected,
        "runtime_release_sha": release_sha, "exception_release_sha": expected,
    }


def _current_exception_preview(session, batch, current, *, release_sha: str, key: str, approval: tuple):
    if not current:
        return None
    return preview_online_abc_exception(
        session, batch.id, current.account_id, runtime_release_sha=release_sha,
        idempotency_key=f"{key}:current:v{current.version}", requested_by=approval[0],
        approved_by=approval[1], approval_ref=approval[2],
    )


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
        raise AuthorizationDrError("online_abc_sweep_not_quiescent", "Global DR boundary is active")


def _require_startable(batch) -> None:
    if batch.selection_mode != "all_online_accounts" or batch.status not in {
        "stopped", "sweep_paused",
    }:
        raise AuthorizationDrError("online_abc_sweep_start_invalid", f"Batch is {batch.status}")


def _approval(
    batch, *, requested_by: str, approved_by: str, approval_ref: str,
) -> tuple[str, str, str]:
    values = tuple(value.strip() for value in (requested_by, approved_by, approval_ref))
    if not all(values) or values[0] == values[1] or values != (
        batch.requested_by, batch.approved_by, batch.approval_ref,
    ):
        raise AuthorizationDrError("online_abc_runner_approval_mismatch", "Sweep approval differs from batch")
    return values


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


def _sweeping_batch(session):
    started = select(AuditLog.id).where(
        AuditLog.target_type == "tg_authorization_online_abc_batches",
        AuditLog.target_id == TgAuthorizationOnlineAbcBatch.id,
        AuditLog.action == START_ACTION,
    ).exists()
    return session.scalar(select(TgAuthorizationOnlineAbcBatch).where(
        TgAuthorizationOnlineAbcBatch.status.in_({"running", "stopped", "sweeping"}),
        started,
    ).order_by(TgAuthorizationOnlineAbcBatch.created_at).limit(1))


def _current_item(session, batch_id: str):
    rows = list(session.scalars(select(TgAuthorizationOnlineAbcItem).where(
        TgAuthorizationOnlineAbcItem.batch_id == batch_id,
        TgAuthorizationOnlineAbcItem.status.in_({"running", "stopped"}),
    )))
    if len(rows) > 1:
        raise AuthorizationDrError("online_abc_exception_item_ambiguous", "Multiple current items exist")
    return rows[0] if rows else None


def _item_counts(session, batch_id: str) -> Counter:
    rows = session.execute(select(
        TgAuthorizationOnlineAbcItem.status, func.count(),
    ).where(TgAuthorizationOnlineAbcItem.batch_id == batch_id).group_by(
        TgAuthorizationOnlineAbcItem.status,
    ))
    return Counter({status: int(count) for status, count in rows})


def _last_checkpoint_total(session, batch_id: str) -> int:
    row = session.scalar(select(AuditLog).where(
        AuditLog.target_type == "tg_authorization_online_abc_batches",
        AuditLog.target_id == batch_id, AuditLog.action == CHECKPOINT_ACTION,
    ).order_by(AuditLog.id.desc()).limit(1))
    if row:
        return _integer_detail(row.detail or "", "terminal_total")
    start = session.scalar(select(AuditLog).where(
        AuditLog.target_type == "tg_authorization_online_abc_batches",
        AuditLog.target_id == batch_id, AuditLog.action == START_ACTION,
    ).order_by(AuditLog.id.desc()).limit(1))
    return _integer_detail(start.detail or "", "baseline_terminal_total") if start else 0


def _audit_start(session, batch, preview: dict) -> None:
    baseline = sum(preview["item_counts"].get(name, 0) for name in TERMINAL_ITEM_STATUSES)
    audit(
        session, tenant_id=batch.tenant_id, actor=preview["approved_by"],
        action=START_ACTION, target_type="tg_authorization_online_abc_batches", target_id=batch.id,
        detail=(
            f"approval_ref={preview['approval_ref']}; idempotency_key={preview['idempotency_key']}; "
            f"fingerprint={preview['fingerprint']}; runtime_release={preview['runtime_release_sha']}; "
            f"execution_release={preview['execution_release_rebind']['previous_release_sha']}->"
            f"{preview['runtime_release_sha']}; "
            f"until_exhausted=true; checkpoint_interval={DEFAULT_CHECKPOINT_INTERVAL}; "
            f"baseline_terminal_total={baseline};"
        ),
    )


def _integer_detail(detail: str, key: str) -> int:
    match = re.search(rf"(?:^|; ){re.escape(key)}=(\d+);", detail)
    return int(match.group(1)) if match else 0


def _existing_start(session, batch_id: str, key: str) -> dict | None:
    normalized = _key(key)
    row = session.scalar(select(AuditLog).where(
        AuditLog.target_type == "tg_authorization_online_abc_batches",
        AuditLog.target_id == batch_id, AuditLog.action == START_ACTION,
        AuditLog.detail.contains(f"idempotency_key={normalized};"),
    ).order_by(AuditLog.id.desc()).limit(1))
    if not row:
        return None
    match = re.search(r"fingerprint=([0-9a-f]{64});", row.detail or "")
    if not match:
        raise AuthorizationDrError("online_abc_sweep_audit_invalid", "Sweep start audit is malformed")
    return {**online_abc_sweep_status(session, batch_id), "fingerprint": match.group(1)}


def _idempotent(existing: dict, expected_fingerprint: str) -> dict:
    if existing["fingerprint"] != expected_fingerprint:
        raise AuthorizationDrError("idempotency_key_conflict", "Sweep start key was already used")
    return {**existing, "already_applied": True}


def _release_sha(value: str) -> str:
    normalized = value.strip().lower()
    if not SHA_PATTERN.fullmatch(normalized):
        raise AuthorizationDrError("runtime_image_mismatch", "Exact current release SHA is required")
    return normalized


def _key(value: str) -> str:
    normalized = value.strip()
    if not KEY_PATTERN.fullmatch(normalized):
        raise AuthorizationDrError("idempotency_key_required", "Sweep idempotency key is invalid")
    return normalized


def _fingerprint(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _error_code(exc: Exception) -> str:
    return exc.code if isinstance(exc, AuthorizationDrError) else type(exc).__name__


__all__ = [
    "apply_online_abc_sweep_start", "online_abc_sweep_status",
    "pause_online_abc_sweep_for_error", "preview_online_abc_sweep_start",
    "readback_online_abc_sweep_start", "run_online_abc_sweep_once",
]

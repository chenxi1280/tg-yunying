from __future__ import annotations

from datetime import timedelta
import json

from sqlalchemy import select

from app.models import (
    AuditLog, AuthorizationDrExecutionNode, TgAccount, TgAccountAuthorization, TgAuthorizationDrOperation,
    TgAuthorizationOnlineAbcBatch, TgAuthorizationOnlineAbcItem, TgPostLoginAbcRequest,
)
from app.services._common import _now, audit

from . import online_abc_exception_queue as exceptions
from .contracts import AuthorizationDrError
from .online_abc import UNKNOWN_OPERATION_STATUSES
from .online_abc_operations import online_abc_item_operations
from .readiness import MY_NODE_STALE_SECONDS


ACTION = "隔离 post-login 单账号 ABC 未知结果"
SELECTION_MODE = "post_login_exact"


def preview_post_login_abc_exception(session, batch_id: str, *, account_id: int, **approval) -> dict:
    context, request = _context(session, batch_id, account_id)
    identity = exceptions._approval(context.batch, **_identity(approval))
    key = exceptions._key(approval["idempotency_key"])
    release = exceptions._release_sha(approval["runtime_release_sha"])
    classification, operation, retired_owner = _classify_unknown(session, context, key)
    if (classification != exceptions.CLASS_DEFERRED_RECONCILE
            or operation.status not in UNKNOWN_OPERATION_STATUSES or operation.remote_call_state != "unknown"):
        raise AuthorizationDrError("post_login_exception_not_unknown", "Only an unknown operation can be isolated")
    global_state = exceptions._global_boundary(session, operation)
    payload = exceptions._payload(
        session, context, classification=classification, operation=operation,
        global_state=global_state, release_sha=release, key=key, approval=identity,
    )
    payload.update(request_id=request.id, request_version=request.request_version,
                   request_status=request.status)
    if retired_owner:
        payload["retired_c_owner"] = retired_owner
    return {**payload, "fingerprint": exceptions._fingerprint(payload)}


def apply_post_login_abc_exception(
    session, batch_id: str, *, account_id: int, expected_fingerprint: str, **approval,
) -> dict:
    existing = _existing(session, batch_id, account_id, key=approval["idempotency_key"])
    if existing:
        if existing["fingerprint"] != expected_fingerprint:
            raise AuthorizationDrError("idempotency_key_conflict", "Exception key was already used")
        return {**existing, "already_applied": True}
    exceptions._lock_context(session, batch_id, account_id)
    context, request = _context(session, batch_id, account_id, lock=True)
    operation_ids = [row.id for row in context.operations.values() if row]
    list(session.scalars(select(TgAuthorizationDrOperation).where(
        TgAuthorizationDrOperation.id.in_(operation_ids),
    ).with_for_update().execution_options(populate_existing=True)))
    c_operation = context.operations["c"]
    if c_operation and c_operation.status == "provision_reconcile_unknown" and c_operation.owner_node_id:
        session.scalar(select(AuthorizationDrExecutionNode).where(
            AuthorizationDrExecutionNode.id == c_operation.owner_node_id,
        ).with_for_update().execution_options(populate_existing=True))
    preview = preview_post_login_abc_exception(session, batch_id, account_id=account_id, **approval)
    if preview["fingerprint"] != expected_fingerprint:
        raise AuthorizationDrError("migration_fingerprint_conflict", "Exception preview changed")
    exceptions._quarantine(session, context, preview)
    context.batch.status = "completed_with_exceptions"
    context.batch.version += 1
    request.status = "reconcile_unknown"
    request.failure_type = "deferred_reconcile"
    request.request_version += 1
    audit(session, tenant_id=context.batch.tenant_id, actor=preview["approved_by"],
          action=ACTION, target_type="tg_authorization_online_abc_items", target_id=context.item.id,
          detail=(f"approval_ref={preview['approval_ref']}; idempotency_key={preview['idempotency_key']}; "
                  f"fingerprint={preview['fingerprint']}; operation={preview['operation_id']}; "
                  f"previous_release={preview['previous_execution_release_sha']}; "
                  f"runtime_release={preview['runtime_release_sha']}; no_replay=true"
                  f"{_retired_owner_audit(preview)}"))
    session.commit()
    return {**_readback(context, request), "fingerprint": expected_fingerprint, "already_applied": False}


def _retired_owner_audit(preview):
    owner = preview.get("retired_c_owner")
    return f"; retired_c_owner={json.dumps(owner, sort_keys=True)}" if owner else ""


def _classify_unknown(session, context, key):
    operation = exceptions._uncertain_operation(context.operations)
    if not operation or not operation.owner_node_id:
        classification, operation = exceptions._classify(session, context, key)
        return classification, operation, {}
    retired_c = (
        operation is context.operations["c"]
        and operation.operation_type == "provision_standby_2"
        and operation.status == "provision_reconcile_unknown"
        and operation.remote_call_state == "unknown"
        and not operation.lease_token and operation.lease_expires_at is None
    )
    if not retired_c:
        raise AuthorizationDrError("online_abc_exception_owner_active", "Operation owner or lease is active")
    owner = _retired_owner_snapshot(session, operation)
    exceptions._require_primary_frozen(context)
    return exceptions.CLASS_DEFERRED_RECONCILE, operation, owner


def _retired_owner_snapshot(session, operation):
    node = session.scalar(select(AuthorizationDrExecutionNode).where(
        AuthorizationDrExecutionNode.id == operation.owner_node_id,
    ).execution_options(populate_existing=True))
    idle = (node and node.region_code == "my" and node.purpose == "standby_session_dr"
            and node.status == "ready" and node.active_client_count == 0)
    cutoff = _now() - timedelta(seconds=MY_NODE_STALE_SECONDS)
    retired = (idle and node.last_heartbeat_at and operation.updated_at
               and node.last_heartbeat_at >= operation.updated_at and node.last_heartbeat_at > cutoff)
    if not retired:
        raise AuthorizationDrError("online_abc_exception_owner_active", "MY owner retirement is not proven")
    # Idle heartbeat renewal is revalidated at apply, but is not a business-state change.
    return {
        "node_id": node.id, "owner_epoch": operation.owner_epoch,
        "region_code": node.region_code, "purpose": node.purpose,
        "runtime_image_sha": node.runtime_image_sha, "status": node.status,
        "active_client_count": node.active_client_count,
        "unknown_updated_at": str(operation.updated_at), "heartbeat_covers_unknown": True,
    }


def _context(session, batch_id: str, account_id: int, *, lock: bool = False):
    batch, item = _stopped_target(session, batch_id, account_id)
    query = select(TgPostLoginAbcRequest).where(
        TgPostLoginAbcRequest.abc_batch_id == batch_id,
        TgPostLoginAbcRequest.account_id == account_id,
        TgPostLoginAbcRequest.tenant_id == batch.tenant_id,
    )
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    requests = list(session.scalars(query))
    if len(requests) != 1 or not _request_matches(requests[0], batch):
        raise AuthorizationDrError("post_login_exception_request_invalid", "Original request approval changed")
    slots = exceptions._slots(session, item.id)
    account = session.get(TgAccount, account_id)
    primary = session.get(TgAccountAuthorization, item.primary_authorization_id)
    if lock:
        account = session.scalar(select(TgAccount).where(TgAccount.id == account_id)
                                 .with_for_update().execution_options(populate_existing=True))
        primary = session.scalar(select(TgAccountAuthorization).where(
            TgAccountAuthorization.id == item.primary_authorization_id,
        ).with_for_update().execution_options(populate_existing=True))
    if not account or not primary or set(slots) != {"standby_1", "standby_2"}:
        raise AuthorizationDrError("post_login_exception_facts_invalid", "Frozen authorization facts are incomplete")
    return exceptions.ExceptionContext(
        batch, item, slots, online_abc_item_operations(session, batch, item), account, primary,
    ), requests[0]


def _stopped_target(session, batch_id: str, account_id: int):
    batch = session.get(TgAuthorizationOnlineAbcBatch, batch_id)
    if not batch or batch.selection_mode != SELECTION_MODE or batch.target_count != 1 or batch.status != "stopped":
        raise AuthorizationDrError("post_login_exception_batch_invalid", "A stopped exact one-account batch is required")
    items = list(session.scalars(select(TgAuthorizationOnlineAbcItem).where(
        TgAuthorizationOnlineAbcItem.batch_id == batch_id,
    )))
    if len(items) != 1 or items[0].account_id != account_id or items[0].status != "stopped":
        raise AuthorizationDrError("post_login_exception_item_invalid", "Exact stopped account changed")
    return batch, items[0]


def _request_matches(request, batch) -> bool:
    fields = ("requested_by", "approved_by", "approval_ref", "deployed_release_sha")
    return all(getattr(request, name) == getattr(batch, name) for name in fields)


def _identity(approval: dict) -> dict:
    return {key: approval[key] for key in ("requested_by", "approved_by", "approval_ref")}


def _existing(session, batch_id: str, account_id: int, *, key: str) -> dict | None:
    normalized = exceptions._key(key)
    item = session.scalar(select(TgAuthorizationOnlineAbcItem).where(
        TgAuthorizationOnlineAbcItem.batch_id == batch_id,
        TgAuthorizationOnlineAbcItem.account_id == account_id,
    ))
    if not item:
        return None
    row = session.scalar(select(AuditLog).where(
        AuditLog.target_type == "tg_authorization_online_abc_items", AuditLog.target_id == item.id,
        AuditLog.action == ACTION, AuditLog.detail.contains(f"idempotency_key={normalized};"),
    ).order_by(AuditLog.id.desc()).limit(1))
    if not row:
        return None
    return {"batch_id": batch_id, "account_id": account_id, "item_outcome": item.outcome,
            "fingerprint": exceptions._audit_value(row.detail, "fingerprint")}


def _readback(context, request) -> dict:
    return {"batch_id": context.batch.id, "account_id": context.item.account_id,
            "batch_status": context.batch.status, "item_outcome": context.item.outcome,
            "request_status": request.status}

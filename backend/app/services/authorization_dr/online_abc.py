from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass

from sqlalchemy import select

from app.models import (
    AccountProxy,
    AuthorizationDrRuntimeContract,
    DeveloperAppSlotAssignment,
    TelegramDeveloperApp,
    TgAccount,
    TgAccountAuthorization,
    TgAuthorizationDrOperation,
    TgAuthorizationOnlineAbcBatch,
    TgAuthorizationOnlineAbcItem,
    TgAuthorizationOnlineAbcSlotResult,
)
from app.services._common import _now, audit

from .contracts import AuthorizationDrError
from .online_abc_operations import online_abc_item_operations, online_abc_operation_keys
from .online_abc_primary import stop_completed_primary_drift
from .online_abc_read import item_operations_complete, operation_outcome, render_online_abc_status


TEN_ACCOUNT_CANARY_SIZE = 10
TERMINAL_FAILURES = {"failed", "manual_required", "migration_rolled_back_forward"}
UNKNOWN_OPERATION_STATUSES = {"provision_reconcile_unknown", "reconcile_unknown"}
ACTIVE_BATCH_STATUSES = {"approved", "running"}
OPEN_BATCH_STATUSES = ACTIVE_BATCH_STATUSES | {"observing"}


@dataclass(frozen=True)
class FrozenOnlineAbcTarget:
    account_id: int
    primary_authorization_id: int
    primary_fact_version: int
    authorization_generation: int
    authorization_fact_generation: int
    connection_generation: int
    primary_session_digest: str
    app_b_id: int
    app_b_credentials_version: int
    app_b_assignment_purpose: str
    app_b_assignment_version: int
    proxy_id: int
    source_c_authorization_id: int
    source_c_fact_version: int
    source_c_slot_generation: int
    standby_1_plan: str
    standby_2_plan: str


def preview_online_abc_batch(
    session,
    tenant_id: int,
    account_ids: list[int],
    *,
    idempotency_key: str,
    deployed_release_sha: str,
) -> dict:
    _require_runtime_off(session)
    _require_no_global_unknown(session)
    normalized = _target_ids(account_ids)
    targets = [_freeze_target(session, tenant_id, account_id) for account_id in normalized]
    body = _preview_body(tenant_id, idempotency_key, deployed_release_sha, targets)
    return {**body, "fingerprint": _fingerprint(body)}


def apply_online_abc_batch(
    session,
    tenant_id: int,
    account_ids: list[int],
    *,
    idempotency_key: str,
    deployed_release_sha: str,
    expected_fingerprint: str,
    requested_by: str,
    approved_by: str,
    approval_ref: str,
) -> dict:
    _require_approval(requested_by, approved_by, approval_ref)
    existing = _batch_by_key(session, tenant_id, idempotency_key)
    if existing:
        return online_abc_batch_status(session, existing.id)
    _require_no_open_batch(session, tenant_id)
    preview = preview_online_abc_batch(
        session, tenant_id, account_ids,
        idempotency_key=idempotency_key,
        deployed_release_sha=deployed_release_sha,
    )
    if preview["fingerprint"] != expected_fingerprint:
        raise AuthorizationDrError("migration_fingerprint_conflict", "Online ABC manifest changed")
    batch = _create_batch(session, preview, requested_by, approved_by, approval_ref)
    _create_items(session, batch, preview["targets"])
    _audit_batch(session, batch, "批准 10 账号 ABC canary manifest", approved_by)
    session.commit()
    return online_abc_batch_status(session, batch.id)


def start_next_online_abc_item(session, batch_id: str, *, actor: str, approval_ref: str) -> dict:
    _require_actor(actor, approval_ref)
    batch = _locked_batch(session, batch_id)
    if batch.status not in ACTIVE_BATCH_STATUSES:
        raise AuthorizationDrError("online_abc_batch_not_runnable", f"Batch is {batch.status}")
    _require_runtime_off(session)
    _stop_on_global_unknown(session, batch)
    if stop_completed_primary_drift(session, batch, actor=actor, approval_ref=approval_ref):
        raise AuthorizationDrError("online_abc_primary_drift", "A completed canary primary drifted")
    running = _running_item(session, batch.id)
    item = running or _next_pending_item(session, batch.id)
    if not item:
        raise AuthorizationDrError("online_abc_batch_complete", "No pending ABC item remains")
    if running is None:
        _start_item(session, batch, item, actor, approval_ref)
        session.commit()
    return _item_command(batch, item)


def sync_online_abc_batch(session, batch_id: str, *, actor: str, approval_ref: str) -> dict:
    _require_actor(actor, approval_ref)
    batch = _locked_batch(session, batch_id)
    if stop_completed_primary_drift(session, batch, actor=actor, approval_ref=approval_ref):
        return online_abc_batch_status(session, batch.id)
    item = _running_item(session, batch.id)
    if item is None:
        return online_abc_batch_status(session, batch.id)
    operations = online_abc_item_operations(session, batch, item)
    _sync_primary_probe(session, item)
    _sync_slot(session, item, "standby_1", operations["b"])
    _sync_slot(session, item, "standby_2", operations["c"])
    terminal = _terminal_outcome(operations)
    if terminal:
        _stop_item(session, batch, item, terminal)
    elif item_operations_complete(session, item, operations):
        _complete_item(session, batch, item, actor, approval_ref)
    session.commit()
    return online_abc_batch_status(session, batch.id)


def online_abc_batch_status(session, batch_id: str) -> dict:
    return render_online_abc_status(session, batch_id)


def _freeze_target(session, tenant_id: int, account_id: int) -> FrozenOnlineAbcTarget:
    account = session.get(TgAccount, account_id)
    if not account or account.tenant_id != tenant_id or account.deleted_at is not None or account.status != "在线":
        raise AuthorizationDrError("online_abc_target_unavailable", f"Account {account_id} is not online")
    primary = _structural_primary(account)
    _require_no_account_operation(session, account_id)
    _require_no_healthy_b(session, account_id)
    source_c = _source_c(session, account_id)
    assignment, app_b, proxy = _backup_route(session, account, primary, source_c)
    return FrozenOnlineAbcTarget(
        account_id=account_id,
        primary_authorization_id=primary.id,
        primary_fact_version=primary.fact_version,
        authorization_generation=account.authorization_generation,
        authorization_fact_generation=account.authorization_fact_generation,
        connection_generation=account.connection_generation,
        primary_session_digest=_digest(primary.session_ciphertext or ""),
        app_b_id=app_b.id,
        app_b_credentials_version=app_b.credentials_version,
        app_b_assignment_purpose=assignment.slot_purpose,
        app_b_assignment_version=assignment.assignment_version,
        proxy_id=proxy.id,
        source_c_authorization_id=source_c.id,
        source_c_fact_version=source_c.fact_version,
        source_c_slot_generation=source_c.slot_generation,
        standby_1_plan="provision",
        standby_2_plan="migrate",
    )


def _structural_primary(account: TgAccount) -> TgAccountAuthorization:
    primary = next((row for row in account.authorizations if row.id == account.current_authorization_id), None)
    valid = (
        primary and primary.is_current and primary.is_slot_current
        and primary.logical_slot == "primary" and primary.provision_region_code == "sv"
        and primary.session_ciphertext == account.session_ciphertext
        and primary.developer_app_id == account.developer_app_id
        and primary.session_ciphertext and primary.protected_from_cleanup
        and primary.disabled_at is None
    )
    if not valid:
        raise AuthorizationDrError("primary_canonical_unproven", "Current A projection is unavailable")
    return primary


def _source_c(session, account_id: int) -> TgAccountAuthorization:
    assignment = session.get(DeveloperAppSlotAssignment, "standby_2_my")
    app_id = assignment.developer_app_id if assignment and assignment.status == "active" else None
    row = session.scalar(select(TgAccountAuthorization).where(
        TgAccountAuthorization.account_id == account_id,
        TgAccountAuthorization.logical_slot == "standby_2",
        TgAccountAuthorization.is_slot_current.is_(True),
        TgAccountAuthorization.provision_region_code == "sv",
        TgAccountAuthorization.developer_app_id == app_id,
        TgAccountAuthorization.health_status == "healthy",
        TgAccountAuthorization.session_ciphertext.is_not(None),
        TgAccountAuthorization.session_ciphertext != "",
        TgAccountAuthorization.protected_from_cleanup.is_(True),
        TgAccountAuthorization.disabled_at.is_(None),
    ).limit(1))
    if not row:
        raise AuthorizationDrError("migration_source_unavailable", "Healthy App C/SV source is unavailable")
    return row


def _backup_route(session, account, primary, source_c):
    proxy = session.get(AccountProxy, account.proxy_id) if account.proxy_id else None
    if not proxy or proxy.status not in {"healthy", "available", "normal", "active"}:
        raise AuthorizationDrError("proxy_unavailable", "A SV proxy is unavailable for B login")
    excluded = {primary.developer_app_id, source_c.developer_app_id}
    for purpose in ("standby_1_sv", "primary_sv"):
        assignment = session.get(DeveloperAppSlotAssignment, purpose)
        app = session.get(TelegramDeveloperApp, assignment.developer_app_id) if assignment else None
        if assignment and assignment.status == "active" and app and app.is_active and app.id not in excluded:
            return assignment, app, proxy
    raise AuthorizationDrError("developer_app_slot_assignment_conflict", "No distinct SV backup App is available")


def _create_batch(session, preview, requested_by, approved_by, approval_ref):
    batch = TgAuthorizationOnlineAbcBatch(
        tenant_id=preview["tenant_id"], idempotency_key=preview["idempotency_key"],
        target_set_fingerprint=preview["fingerprint"], target_count=preview["target_count"],
        deployed_release_sha=preview["deployed_release_sha"],
        execution_release_sha=preview["deployed_release_sha"], status="approved",
        selection_mode=preview.get("selection_mode", "exact_ten_canary"),
        requested_by=requested_by, approved_by=approved_by, approval_ref=approval_ref,
        approved_at=_now(),
    )
    session.add(batch)
    session.flush()
    return batch


def _create_items(session, batch, targets) -> None:
    for ordinal, target in enumerate(targets, start=1):
        item = TgAuthorizationOnlineAbcItem(
            batch_id=batch.id, tenant_id=batch.tenant_id, ordinal=ordinal,
            status="pending", outcome="pending", primary_probe_outcome="pending", **target,
        )
        session.add(item)
        session.flush()
        for logical_slot in ("standby_1", "standby_2"):
            plan = target[f"{logical_slot}_plan"]
            session.add(TgAuthorizationOnlineAbcSlotResult(
                batch_id=batch.id, item_id=item.id, tenant_id=batch.tenant_id,
                account_id=item.account_id, logical_slot=logical_slot,
                outcome="already_qualified" if plan == "already_qualified" else "pending",
            ))


def _start_item(session, batch, item, actor, approval_ref) -> None:
    item.status = "running"
    item.outcome = "running"
    item.started_at = _now()
    item.version += 1
    batch.status = "running"
    batch.version += 1
    audit(session, tenant_id=batch.tenant_id, actor=actor, action="启动单个 ABC canary item",
          target_type="tg_authorization_online_abc_items", target_id=item.id,
          detail=f"account_id={item.account_id}; approval_ref={approval_ref}")


def _sync_primary_probe(session, item) -> None:
    account = session.get(TgAccount, item.account_id)
    primary = session.get(TgAccountAuthorization, item.primary_authorization_id)
    valid = (
        account and primary and account.current_authorization_id == primary.id
        and _digest(primary.session_ciphertext or "") == item.primary_session_digest
        and account.authorization_generation == item.authorization_generation
        and account.connection_generation == item.connection_generation
        and account.authorization_fact_generation == item.authorization_fact_generation + 1
        and primary.fact_version == item.primary_fact_version + 1
        and primary.telegram_user_id_digest and primary.auth_key_fingerprint_digest
    )
    item.primary_probe_outcome = "succeeded" if valid else "pending"


def _sync_slot(session, item, logical_slot: str, operation) -> None:
    slot = session.scalar(select(TgAuthorizationOnlineAbcSlotResult).where(
        TgAuthorizationOnlineAbcSlotResult.item_id == item.id,
        TgAuthorizationOnlineAbcSlotResult.logical_slot == logical_slot,
    ))
    if operation is None:
        return
    slot.operation_id = operation.id
    slot.outcome = operation_outcome(operation.status)
    slot.blocker_code = operation.blocker_code
    slot.version += 1


def _complete_item(session, batch, item, actor, approval_ref) -> None:
    if item.primary_probe_outcome != "succeeded":
        _stop_item(session, batch, item, "primary_probe_failed")
        return
    item.status = "succeeded"
    item.outcome = "succeeded"
    item.finished_at = _now()
    item.version += 1
    _audit_batch(session, batch, f"完成 ABC canary account={item.account_id}", actor, approval_ref)
    if all(value.outcome == "succeeded" for value in _items(session, batch.id)):
        if batch.selection_mode == "exact_ten_canary":
            batch.status = "observing"
            batch.observation_started_at = _now()
            batch.observation_closes_at = batch.observation_started_at
        else:
            batch.status = "completed"
    batch.version += 1


def _stop_item(session, batch, item, outcome: str) -> None:
    item.status = "stopped"
    item.outcome = outcome
    item.blocker_code = outcome
    item.finished_at = _now()
    item.version += 1
    batch.status = "stopped"
    batch.version += 1


def _terminal_outcome(operations: dict) -> str:
    for operation in operations.values():
        if operation and operation.status in UNKNOWN_OPERATION_STATUSES:
            return "reconcile_unknown"
    for operation in operations.values():
        if operation and operation.status in TERMINAL_FAILURES:
            return operation.status
    return ""


def _item_command(batch, item) -> dict:
    keys = online_abc_operation_keys(batch, item)
    return {
        "batch_id": batch.id, "item_id": item.id, "ordinal": item.ordinal,
        "account_id": item.account_id, "b_idempotency_key": keys["b"],
        "c_idempotency_key": keys["c"], "e4_idempotency_key": keys["e4"],
        "primary_session_digest": item.primary_session_digest,
    }


def _preview_body(tenant_id, key, release_sha, targets) -> dict:
    normalized_key = key.strip()
    if not normalized_key:
        raise AuthorizationDrError("idempotency_key_required", "Online ABC idempotency key is required")
    release = release_sha.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", release):
        raise AuthorizationDrError("runtime_image_mismatch", "Exact deployed release SHA is required")
    return {
        "tenant_id": tenant_id, "idempotency_key": normalized_key,
        "target_count": len(targets), "deployed_release_sha": release,
        "targets": [dict(target) if isinstance(target, dict) else asdict(target) for target in targets],
    }


def _target_ids(account_ids: list[int]) -> list[int]:
    normalized = [int(value) for value in account_ids]
    if len(normalized) != TEN_ACCOUNT_CANARY_SIZE or len(set(normalized)) != TEN_ACCOUNT_CANARY_SIZE:
        raise AuthorizationDrError("online_abc_target_count_invalid", "Exactly 10 unique accounts are required")
    return normalized


def _require_runtime_off(session) -> None:
    contract = session.get(AuthorizationDrRuntimeContract, 1)
    if not contract or contract.mode != "off" or contract.claim_scope_operation_id:
        raise AuthorizationDrError("runtime_capability_unproven", "DR runtime is not safely off")


def _require_no_global_unknown(session) -> None:
    unknown = session.scalar(select(TgAuthorizationDrOperation.id).where(
        TgAuthorizationDrOperation.status.in_(UNKNOWN_OPERATION_STATUSES),
    ).limit(1))
    if unknown:
        raise AuthorizationDrError("global_reconcile_unknown", "Global reconcile unknown must be zero")


def _require_no_open_batch(session, tenant_id: int) -> None:
    batch = session.scalar(select(TgAuthorizationOnlineAbcBatch.id).where(
        TgAuthorizationOnlineAbcBatch.tenant_id == tenant_id,
        TgAuthorizationOnlineAbcBatch.status.in_(OPEN_BATCH_STATUSES),
    ).limit(1))
    if batch:
        raise AuthorizationDrError("online_abc_batch_active", "An online ABC batch is already open")


def _stop_on_global_unknown(session, batch) -> None:
    try:
        _require_no_global_unknown(session)
    except AuthorizationDrError:
        batch.status = "stopped"
        batch.version += 1
        session.commit()
        raise


def _require_no_healthy_b(session, account_id: int) -> None:
    row = session.scalar(select(TgAccountAuthorization.id).where(
        TgAccountAuthorization.account_id == account_id,
        TgAccountAuthorization.logical_slot == "standby_1",
        TgAccountAuthorization.is_slot_current.is_(True),
        TgAccountAuthorization.health_status == "healthy",
        TgAccountAuthorization.disabled_at.is_(None),
    ).limit(1))
    if row:
        raise AuthorizationDrError("sv_redundancy_already_ready", "Account already has healthy B")


def _require_no_account_operation(session, account_id: int) -> None:
    row = session.scalar(select(TgAuthorizationDrOperation.id).where(
        TgAuthorizationDrOperation.account_id == account_id,
    ).limit(1))
    if row:
        raise AuthorizationDrError("authorization_operation_exists", "Account already has a DR operation")


def _locked_batch(session, batch_id: str):
    batch = session.scalar(select(TgAuthorizationOnlineAbcBatch).where(
        TgAuthorizationOnlineAbcBatch.id == batch_id,
    ).with_for_update())
    if not batch:
        raise AuthorizationDrError("online_abc_batch_not_found", "Online ABC batch is unavailable")
    return batch


def _batch_by_key(session, tenant_id: int, key: str):
    return session.scalar(select(TgAuthorizationOnlineAbcBatch).where(
        TgAuthorizationOnlineAbcBatch.tenant_id == tenant_id,
        TgAuthorizationOnlineAbcBatch.idempotency_key == key.strip(),
    ))


def _items(session, batch_id: str):
    return list(session.scalars(select(TgAuthorizationOnlineAbcItem).where(
        TgAuthorizationOnlineAbcItem.batch_id == batch_id,
    ).order_by(TgAuthorizationOnlineAbcItem.ordinal)))


def _slots(session, batch_id: str):
    return list(session.scalars(select(TgAuthorizationOnlineAbcSlotResult).where(
        TgAuthorizationOnlineAbcSlotResult.batch_id == batch_id,
    ).order_by(TgAuthorizationOnlineAbcSlotResult.account_id, TgAuthorizationOnlineAbcSlotResult.logical_slot)))


def _running_item(session, batch_id: str):
    return session.scalar(select(TgAuthorizationOnlineAbcItem).where(
        TgAuthorizationOnlineAbcItem.batch_id == batch_id,
        TgAuthorizationOnlineAbcItem.status == "running",
    ).limit(1))


def _next_pending_item(session, batch_id: str):
    return session.scalar(select(TgAuthorizationOnlineAbcItem).where(
        TgAuthorizationOnlineAbcItem.batch_id == batch_id,
        TgAuthorizationOnlineAbcItem.status == "pending",
    ).order_by(TgAuthorizationOnlineAbcItem.ordinal).limit(1))


def _audit_batch(session, batch, action: str, actor: str, approval_ref: str = "") -> None:
    audit(session, tenant_id=batch.tenant_id, actor=actor, action=action,
          target_type="tg_authorization_online_abc_batches", target_id=batch.id,
          detail=f"approval_ref={approval_ref or batch.approval_ref}; target_count={batch.target_count}")


def _require_approval(requested_by: str, approved_by: str, approval_ref: str) -> None:
    if not requested_by.strip() or not approved_by.strip() or not approval_ref.strip():
        raise AuthorizationDrError("approval_ref_required", "Requester, approver and approval ref are required")
    if requested_by.strip() == approved_by.strip():
        raise AuthorizationDrError("approval_actor_conflict", "Approver must differ from requester")


def _require_actor(actor: str, approval_ref: str) -> None:
    if not actor.strip() or not approval_ref.strip():
        raise AuthorizationDrError("approval_ref_required", "Actor and approval ref are required")


def _fingerprint(value: dict) -> str:
    return _digest(json.dumps(value, sort_keys=True, separators=(",", ":")))


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


__all__ = [
    "apply_online_abc_batch", "online_abc_batch_status", "preview_online_abc_batch",
    "start_next_online_abc_item", "sync_online_abc_batch",
]

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import or_, select

from app.models import (
    TelegramDeveloperApp,
    TgAccount,
    TgAccountAuthorization,
    TgAuthorizationDrBatch,
    TgAuthorizationDrBatchItem,
    TgAuthorizationDrOperation,
)
from app.services._common import _now, audit, gateway, get_account_phone
from app.services.account_two_fa import managed_two_fa_password
from app.services.developer_apps import credentials_for_account, credentials_for_developer_app

from .contracts import AuthorizationDrError, OperationClaim
from .readiness import require_migration_readiness


CLAIM_LEASE_SECONDS = 90
CLAIMABLE_STATUSES = ("pending", "waiting_login")


def preview_migration_batch(
    session,
    tenant_id: int,
    account_ids: list[int],
    *,
    idempotency_key: str,
    actor: str,
) -> TgAuthorizationDrBatch:
    normalized = sorted(set(account_ids))
    if not normalized:
        raise AuthorizationDrError("empty_target_set", "At least one account is required")
    existing = session.scalar(select(TgAuthorizationDrBatch).where(
        TgAuthorizationDrBatch.tenant_id == tenant_id,
        TgAuthorizationDrBatch.idempotency_key == idempotency_key,
    ))
    fingerprint = _target_fingerprint(tenant_id, normalized)
    if existing:
        if existing.target_set_fingerprint != fingerprint:
            raise AuthorizationDrError("migration_fingerprint_conflict", "Idempotency key target set changed")
        return existing
    sources = [_migration_source(session, tenant_id, account_id) for account_id in normalized]
    batch = TgAuthorizationDrBatch(
        tenant_id=tenant_id,
        idempotency_key=idempotency_key,
        target_set_fingerprint=fingerprint,
        target_count=len(normalized),
        requested_by=actor,
    )
    session.add(batch)
    session.flush()
    for ordinal, source in enumerate(sources, start=1):
        session.add(_batch_item(batch, source, ordinal))
    audit(
        session,
        tenant_id=tenant_id,
        actor=actor,
        action="预览 MY standby_2 迁移",
        target_type="tg_authorization_dr_batch",
        target_id=batch.id,
        detail=f"target_count={len(normalized)}; fingerprint={fingerprint}",
    )
    session.commit()
    return batch


def approve_migration_batch(
    session,
    batch_id: str,
    *,
    expected_version: int,
    approval_ref: str,
    actor: str,
) -> TgAuthorizationDrBatch:
    batch = session.scalar(select(TgAuthorizationDrBatch).where(
        TgAuthorizationDrBatch.id == batch_id,
    ).with_for_update())
    if not batch:
        raise AuthorizationDrError("migration_batch_not_found", "Migration batch does not exist")
    if batch.requested_by == actor:
        raise AuthorizationDrError("approval_actor_conflict", "Approver must differ from requester")
    if batch.version != expected_version or batch.status != "previewed":
        raise AuthorizationDrError("authorization_version_conflict", "Migration batch version changed")
    if not approval_ref.strip():
        raise AuthorizationDrError("approval_ref_required", "Approval reference is required")
    readiness = require_migration_readiness(session)
    _create_operations(
        session,
        batch,
        readiness=readiness,
        approver=actor,
        approval_ref=approval_ref.strip(),
    )
    batch.status = "approved"
    batch.approved_by = actor
    batch.approval_ref = approval_ref.strip()
    batch.approved_at = _now()
    batch.version += 1
    session.commit()
    return batch


def claim_migration_operation(session, node_id: str) -> OperationClaim | None:
    readiness = require_migration_readiness(session)
    if readiness.node.id != node_id:
        raise AuthorizationDrError("execution_node_mismatch", "Operation can only be claimed by the ready MY node")
    now = _now()
    active = session.scalar(select(TgAuthorizationDrOperation.id).where(
        TgAuthorizationDrOperation.owner_node_id != "",
        TgAuthorizationDrOperation.lease_expires_at > now,
        TgAuthorizationDrOperation.status.not_in(("succeeded", "failed", "manual_required")),
    ).limit(1))
    if active:
        return None
    operation = session.scalar(select(TgAuthorizationDrOperation).where(
        TgAuthorizationDrOperation.status.in_(CLAIMABLE_STATUSES),
        or_(
            TgAuthorizationDrOperation.lease_expires_at.is_(None),
            TgAuthorizationDrOperation.lease_expires_at <= now,
        ),
    ).order_by(TgAuthorizationDrOperation.created_at, TgAuthorizationDrOperation.id).limit(1).with_for_update(skip_locked=True))
    if not operation:
        return None
    _verify_frozen_inputs(session, operation, readiness)
    operation.owner_node_id = node_id
    operation.owner_epoch += 1
    operation.lease_token = uuid4().hex
    operation.lease_expires_at = now + timedelta(seconds=CLAIM_LEASE_SECONDS)
    operation.status = "waiting_login"
    operation.operation_version += 1
    _mark_item(session, operation, "running")
    session.commit()
    return _claim_contract(operation)


def mark_login_remote_started(
    session,
    operation_id: str,
    *,
    node_id: str,
    owner_epoch: int,
    lease_token: str,
) -> TgAuthorizationDrOperation:
    operation = _owned_operation(
        session,
        operation_id,
        node_id=node_id,
        owner_epoch=owner_epoch,
        lease_token=lease_token,
    )
    if operation.remote_call_state not in ("none", "started"):
        raise AuthorizationDrError("provision_reconcile_unknown", "Remote login call requires reconciliation")
    operation.remote_call_state = "started"
    operation.remote_effect_started_at = operation.remote_effect_started_at or _now()
    operation.status = "login_remote_started"
    operation.operation_version += 1
    session.commit()
    return operation


def renew_migration_lease(
    session,
    operation_id: str,
    *,
    node_id: str,
    owner_epoch: int,
    lease_token: str,
) -> TgAuthorizationDrOperation:
    operation = _owned_operation(
        session,
        operation_id,
        node_id=node_id,
        owner_epoch=owner_epoch,
        lease_token=lease_token,
    )
    operation.lease_expires_at = _now() + timedelta(seconds=CLAIM_LEASE_SECONDS)
    operation.operation_version += 1
    session.commit()
    return operation


def migration_login_material(
    session,
    operation_id: str,
    *,
    node_id: str,
    owner_epoch: int,
    lease_token: str,
) -> dict:
    operation = _owned_operation(
        session,
        operation_id,
        node_id=node_id,
        owner_epoch=owner_epoch,
        lease_token=lease_token,
    )
    account = session.get(TgAccount, operation.account_id)
    app = session.get(TelegramDeveloperApp, operation.developer_app_id)
    if not account or not app:
        raise AuthorizationDrError("migration_login_material_missing", "Frozen login material is unavailable")
    credentials = credentials_for_developer_app(app)
    return {
        "phone": get_account_phone(account),
        "password_2fa": managed_two_fa_password(session, account) or "",
        "api_id": credentials.api_id,
        "api_hash": credentials.api_hash,
        "app_name": credentials.app_name,
        "credentials_version": credentials.credentials_version,
    }


def poll_migration_login_code(
    session,
    operation_id: str,
    *,
    node_id: str,
    owner_epoch: int,
    lease_token: str,
) -> str:
    operation = _owned_operation(
        session,
        operation_id,
        node_id=node_id,
        owner_epoch=owner_epoch,
        lease_token=lease_token,
    )
    if operation.remote_call_state != "started":
        raise AuthorizationDrError("login_code_challenge_mismatch", "Login challenge has not started")
    account = session.get(TgAccount, operation.account_id)
    if not account or not account.session_ciphertext:
        raise AuthorizationDrError("login_code_not_found", "Current SV code source is unavailable")
    snapshots = gateway.poll_verification_codes(
        account.id,
        session_ciphertext=account.session_ciphertext,
        credentials=credentials_for_account(session, account),
    )
    return snapshots[0].code if snapshots else ""


def mark_login_remote_unknown(session, operation_id: str, *, node_id: str, owner_epoch: int) -> None:
    operation = session.get(TgAuthorizationDrOperation, operation_id)
    if not operation or operation.owner_node_id != node_id or operation.owner_epoch != owner_epoch:
        raise AuthorizationDrError("execution_node_mismatch", "Operation owner changed")
    operation.remote_call_state = "unknown"
    operation.status = "provision_reconcile_unknown"
    operation.blocker_code = "provision_reconcile_unknown"
    operation.lease_token = ""
    operation.lease_expires_at = None
    operation.operation_version += 1
    _mark_item(session, operation, "reconcile_unknown", blocker="provision_reconcile_unknown")
    session.commit()


def _migration_source(session, tenant_id: int, account_id: int) -> TgAccountAuthorization:
    account = session.get(TgAccount, account_id)
    if not account or account.tenant_id != tenant_id or account.deleted_at is not None:
        raise AuthorizationDrError("account_not_found", f"Account {account_id} is unavailable")
    rows = list(session.scalars(select(TgAccountAuthorization).where(
        TgAccountAuthorization.account_id == account_id,
        TgAccountAuthorization.logical_slot == "standby_2",
        TgAccountAuthorization.is_slot_current.is_(True),
    )))
    if len(rows) != 1:
        raise AuthorizationDrError("migration_source_standby_not_unique", f"Account {account_id} source is not unique")
    source = rows[0]
    if not _is_healthy_sv_standby(source):
        raise AuthorizationDrError("migration_source_standby_not_unique", f"Account {account_id} has no SV source Session")
    _require_sv_redundancy(session, account, source)
    return source


def _require_sv_redundancy(session, account: TgAccount, source: TgAccountAuthorization) -> None:
    if not account.session_ciphertext or not account.developer_app_id:
        raise AuthorizationDrError("sv_redundancy_incomplete", f"Account {account.id} current SV Session is unavailable")
    rows = list(session.scalars(select(TgAccountAuthorization).where(
        TgAccountAuthorization.account_id == account.id,
        TgAccountAuthorization.logical_slot == "standby_1",
        TgAccountAuthorization.is_slot_current.is_(True),
        TgAccountAuthorization.disabled_at.is_(None),
    )))
    if len(rows) != 1 or not _is_healthy_sv_standby(rows[0]):
        raise AuthorizationDrError("sv_redundancy_incomplete", f"Account {account.id} has no healthy SV standby_1")
    app_ids = {account.developer_app_id, rows[0].developer_app_id, source.developer_app_id}
    if None in app_ids or len(app_ids) != 3:
        raise AuthorizationDrError("developer_app_slot_assignment_conflict", "Account authorization slots must use three distinct apps")


def _is_healthy_sv_standby(row: TgAccountAuthorization) -> bool:
    return bool(
        row.provision_region_code == "sv"
        and row.session_ciphertext
        and row.status in {"active", "standby"}
        and row.health_status == "healthy"
    )


def _batch_item(batch, source: TgAccountAuthorization, ordinal: int) -> TgAuthorizationDrBatchItem:
    return TgAuthorizationDrBatchItem(
        batch_id=batch.id,
        tenant_id=batch.tenant_id,
        account_id=source.account_id,
        ordinal=ordinal,
        expected_source_authorization_id=source.id,
        expected_source_fact_version=source.fact_version,
        expected_source_generation=source.slot_generation,
        target_generation=source.slot_generation + 1,
    )


def _create_operations(session, batch, *, readiness, approver: str, approval_ref: str) -> None:
    app = session.get(TelegramDeveloperApp, readiness.standby_assignment.developer_app_id)
    if not app or not app.is_active or app.credentials_version != readiness.standby_assignment.credentials_version:
        raise AuthorizationDrError("developer_app_slot_assignment_conflict", "App C credentials changed")
    items = list(session.scalars(select(TgAuthorizationDrBatchItem).where(
        TgAuthorizationDrBatchItem.batch_id == batch.id,
    ).order_by(TgAuthorizationDrBatchItem.ordinal).with_for_update()))
    for item in items:
        source = session.get(TgAccountAuthorization, item.expected_source_authorization_id)
        _verify_source(item, source)
        account = session.get(TgAccount, item.account_id)
        if not account:
            raise AuthorizationDrError("account_not_found", f"Account {item.account_id} is unavailable")
        _require_sv_redundancy(session, account, source)
        if source.developer_app_id != app.id:
            raise AuthorizationDrError("developer_app_slot_assignment_conflict", "Frozen standby_2 does not use App C")
        operation = _new_operation(
            batch,
            item,
            source=source,
            app=app,
            readiness=readiness,
            approver=approver,
            approval_ref=approval_ref,
        )
        session.add(operation)
        session.flush()
        item.operation_id = operation.id
        item.status = "pending"
        item.version += 1


def _new_operation(batch, item, *, source, app, readiness, approver: str, approval_ref: str):
    fingerprint = _operation_fingerprint(batch, item, readiness)
    return TgAuthorizationDrOperation(
        tenant_id=batch.tenant_id,
        account_id=item.account_id,
        batch_item_id=item.id,
        operation_type="migrate_standby_2",
        logical_slot="standby_2",
        source_authorization_id=source.id,
        source_generation=item.expected_source_generation,
        target_generation=item.target_generation,
        developer_app_id=app.id,
        developer_app_api_id_snapshot=app.api_id,
        developer_app_credentials_version=app.credentials_version,
        assignment_version=readiness.assignment_version,
        egress_id=readiness.egress.id,
        egress_version=readiness.egress.version,
        idempotency_key=f"{batch.id}:{item.account_id}:{item.target_generation}",
        request_fingerprint=fingerprint,
        status="pending",
        requested_by=batch.requested_by,
        approved_by=approver,
        approval_ref=approval_ref,
    )


def _verify_frozen_inputs(session, operation, readiness) -> None:
    source = session.get(TgAccountAuthorization, operation.source_authorization_id)
    item = session.get(TgAuthorizationDrBatchItem, operation.batch_item_id)
    _verify_source(item, source)
    if operation.assignment_version != readiness.assignment_version:
        raise AuthorizationDrError("assignment_version_conflict", "Frozen App assignment changed")
    if operation.egress_id != readiness.egress.id or operation.egress_version != readiness.egress.version:
        raise AuthorizationDrError("fixed_egress_version_conflict", "Frozen MY egress changed")


def _verify_source(item, source) -> None:
    if not item or not source:
        raise AuthorizationDrError("migration_source_standby_not_unique", "Frozen migration source is missing")
    matches = (
        source.id == item.expected_source_authorization_id
        and source.fact_version == item.expected_source_fact_version
        and source.slot_generation == item.expected_source_generation
        and source.logical_slot == "standby_2"
        and source.is_slot_current
        and source.provision_region_code == "sv"
    )
    if not matches:
        raise AuthorizationDrError("authorization_version_conflict", "Frozen migration source changed")


def _owned_operation(session, operation_id: str, *, node_id: str, owner_epoch: int, lease_token: str):
    operation = session.scalar(select(TgAuthorizationDrOperation).where(
        TgAuthorizationDrOperation.id == operation_id,
    ).with_for_update())
    if not operation or operation.owner_node_id != node_id or operation.owner_epoch != owner_epoch:
        raise AuthorizationDrError("execution_node_mismatch", "Operation owner changed")
    if operation.lease_token != lease_token or not operation.lease_expires_at or operation.lease_expires_at <= _now():
        raise AuthorizationDrError("malaysia_owner_fencing_unproven", "Operation lease is stale")
    return operation


def _mark_item(session, operation, status: str, *, blocker: str = "") -> None:
    item = session.get(TgAuthorizationDrBatchItem, operation.batch_item_id)
    if item:
        item.status = status
        item.blocker_code = blocker
        item.version += 1


def _claim_contract(operation) -> OperationClaim:
    return OperationClaim(
        operation.id,
        operation.account_id,
        operation.owner_node_id,
        operation.owner_epoch,
        operation.lease_token,
        operation.lease_expires_at,
        operation.target_generation,
        operation.developer_app_id,
        operation.developer_app_api_id_snapshot,
        operation.developer_app_credentials_version,
        operation.egress_id,
        operation.egress_version,
    )


def _target_fingerprint(tenant_id: int, account_ids: list[int]) -> str:
    payload = json.dumps({"tenant_id": tenant_id, "account_ids": account_ids}, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def _operation_fingerprint(batch, item, readiness) -> str:
    payload = {
        "batch": batch.id,
        "account": item.account_id,
        "source": item.expected_source_authorization_id,
        "source_fact": item.expected_source_fact_version,
        "target_generation": item.target_generation,
        "assignment": readiness.assignment_version,
        "egress": [readiness.egress.id, readiness.egress.version],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


__all__ = [
    "approve_migration_batch",
    "claim_migration_operation",
    "mark_login_remote_started",
    "mark_login_remote_unknown",
    "migration_login_material",
    "poll_migration_login_code",
    "preview_migration_batch",
    "renew_migration_lease",
]

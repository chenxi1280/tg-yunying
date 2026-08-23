from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import or_, select

from app.models import (
    AuthorizationDrRuntimeContract,
    TelegramDeveloperApp,
    TgAccount,
    TgAccountAuthorization,
    TgAuthorizationDrBatch,
    TgAuthorizationDrBatchItem,
    TgAuthorizationDrOperation,
)
from app.services._common import _now, audit, gateway, get_account_phone
from app.services.account_two_fa import managed_two_fa_password
from app.services.developer_apps import (
    credentials_for_authorization,
    credentials_for_developer_app,
)
from app.timezone import as_beijing_aware

from .contracts import AuthorizationDrError, OperationClaim
from .login_code import bind_login_code
from .migration_results import (
    mark_login_remote_failed,
    mark_login_remote_unknown,
    refresh_migration_batch,
)
from .operation_state import mark_item as _mark_item, owned_operation as _owned_operation
from .primary_fence import require_primary_code_source, verified_code_source
from .readiness import require_migration_readiness
from .stage_facts import append_stage_fact


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
    readiness = require_migration_readiness(session, require_mode=False)
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
    contract = session.get(AuthorizationDrRuntimeContract, 1)
    if not contract or contract.mode != "migrate":
        return None
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
    filters = [
        TgAuthorizationDrOperation.status.in_(CLAIMABLE_STATUSES),
        or_(
            TgAuthorizationDrOperation.lease_expires_at.is_(None),
            TgAuthorizationDrOperation.lease_expires_at <= now,
        ),
    ]
    if contract.claim_scope_operation_id:
        filters.append(TgAuthorizationDrOperation.id == contract.claim_scope_operation_id)
    operation = session.scalar(select(TgAuthorizationDrOperation).where(*filters).order_by(
        TgAuthorizationDrOperation.created_at,
        TgAuthorizationDrOperation.id,
    ).limit(1).with_for_update(skip_locked=True))
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
    operation.login_challenge_sent_at = operation.login_challenge_sent_at or as_beijing_aware(_now())
    operation.status = "login_remote_started"
    operation.operation_version += 1
    digest = hashlib.sha256(f"{operation.id}:{operation.owner_epoch}:remote_login_started".encode()).hexdigest()
    append_stage_fact(
        session,
        operation,
        stage="remote_login_started",
        manifest_digest=digest,
        evidence_manifest={},
    )
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
    source = verified_code_source(session, operation)
    snapshots = gateway.poll_verification_codes(
        operation.account_id,
        session_ciphertext=source.session_ciphertext,
        credentials=credentials_for_authorization(session, source),
    )
    bound = bind_login_code(
        snapshots,
        challenge_sent_at=operation.login_challenge_sent_at,
        expected_message_id=operation.login_code_message_id,
    )
    if not bound:
        return ""
    if not operation.login_code_message_id:
        operation.login_code_message_id = bound.message_id
        operation.login_code_received_at = as_beijing_aware(bound.received_at)
        operation.operation_version += 1
        session.commit()
    return bound.code


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
    primary = session.get(TgAccountAuthorization, account.current_authorization_id)
    if not primary:
        raise AuthorizationDrError("sv_redundancy_incomplete", f"Account {account.id} current A is unavailable")
    rows = list(session.scalars(select(TgAccountAuthorization).where(
        TgAccountAuthorization.account_id == account.id,
        TgAccountAuthorization.id != account.current_authorization_id,
        TgAccountAuthorization.logical_slot == (
            "primary" if primary.logical_slot == "standby_1" else "standby_1"
        ),
        TgAccountAuthorization.is_slot_current.is_(True),
        TgAccountAuthorization.is_current.is_(False),
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
            account=account,
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


def _new_operation(batch, item, *, account, source, app, readiness, approver: str, approval_ref: str):
    code_source = require_primary_code_source(account)
    fingerprint = _operation_fingerprint(batch, item, readiness, account=account, code_source=code_source)
    return TgAuthorizationDrOperation(
        tenant_id=batch.tenant_id,
        account_id=item.account_id,
        batch_item_id=item.id,
        operation_type="migrate_standby_2",
        logical_slot="standby_2",
        source_authorization_id=source.id,
        code_source_authorization_id=code_source.id,
        source_generation=item.expected_source_generation,
        target_generation=item.target_generation,
        expected_current_authorization_id=account.current_authorization_id,
        expected_authorization_generation=account.authorization_generation,
        expected_authorization_fact_generation=account.authorization_fact_generation,
        expected_connection_generation=account.connection_generation,
        expected_code_source_fact_version=code_source.fact_version,
        expected_code_source_user_id_digest=code_source.telegram_user_id_digest,
        expected_code_source_auth_key_digest=code_source.auth_key_fingerprint_digest,
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
    source = (
        session.get(TgAccountAuthorization, operation.source_authorization_id)
        if operation.source_authorization_id else None
    )
    item = session.get(TgAuthorizationDrBatchItem, operation.batch_item_id)
    if operation.operation_type == "provision_standby_2":
        _verify_provision_source(session, item, source)
    else:
        _verify_source(item, source)
    verified_code_source(session, operation)
    if operation.assignment_version != readiness.assignment_version:
        raise AuthorizationDrError("assignment_version_conflict", "Frozen App assignment changed")
    if operation.egress_id != readiness.egress.id or operation.egress_version != readiness.egress.version:
        raise AuthorizationDrError("fixed_egress_version_conflict", "Frozen MY egress changed")


def _verify_provision_source(session, item, source) -> None:
    if not item:
        raise AuthorizationDrError("migration_source_standby_not_unique", "Frozen provision item is missing")
    if item.expected_source_authorization_id is None:
        current = session.scalar(select(TgAccountAuthorization.id).where(
            TgAccountAuthorization.account_id == item.account_id,
            TgAccountAuthorization.logical_slot == "standby_2",
            TgAccountAuthorization.is_slot_current.is_(True),
            TgAccountAuthorization.disabled_at.is_(None),
        ).limit(1))
        if current:
            raise AuthorizationDrError("authorization_version_conflict", "C slot appeared after preview")
        return
    matches = (
        source
        and source.id == item.expected_source_authorization_id
        and source.fact_version == item.expected_source_fact_version
        and source.slot_generation == item.expected_source_generation
        and source.logical_slot == "standby_2"
        and source.is_slot_current
    )
    if not matches:
        raise AuthorizationDrError("authorization_version_conflict", "Frozen C replacement source changed")


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


def _operation_fingerprint(batch, item, readiness, *, account, code_source) -> str:
    payload = {
        "batch": batch.id,
        "account": item.account_id,
        "source": item.expected_source_authorization_id,
        "source_fact": item.expected_source_fact_version,
        "target_generation": item.target_generation,
        "assignment": readiness.assignment_version,
        "egress": [readiness.egress.id, readiness.egress.version],
        "current": [
            account.current_authorization_id,
            account.authorization_generation,
            account.authorization_fact_generation,
            account.connection_generation,
        ],
        "code_source": [
            code_source.id,
            code_source.fact_version,
            code_source.telegram_user_id_digest,
            code_source.auth_key_fingerprint_digest,
        ],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


__all__ = [
    "approve_migration_batch",
    "claim_migration_operation",
    "mark_login_remote_failed",
    "mark_login_remote_started",
    "mark_login_remote_unknown",
    "migration_login_material",
    "poll_migration_login_code",
    "preview_migration_batch",
    "refresh_migration_batch",
    "renew_migration_lease",
]

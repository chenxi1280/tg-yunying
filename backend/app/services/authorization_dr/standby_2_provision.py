from __future__ import annotations

import hashlib
import json

from sqlalchemy import func, select

from app.models import (
    TelegramDeveloperApp,
    TgAccount,
    TgAccountAuthorization,
    TgAuthorizationDrBatch,
    TgAuthorizationDrBatchItem,
    TgAuthorizationDrOperation,
)
from app.services._common import _now, audit

from .abc_canary import _arm_exact_operation, _valid_image_sha
from .contracts import AuthorizationDrError
from .primary_fence import require_primary_code_source
from .readiness import require_migration_readiness


def prepare_scoped_c_provision(
    session,
    tenant_id: int,
    account_id: int,
    *,
    idempotency_key: str,
    requested_by: str,
    approved_by: str,
    approval_ref: str,
    runtime_image_sha: str,
) -> dict:
    _require_approval(requested_by, approved_by, approval_ref)
    image_sha = _valid_image_sha(runtime_image_sha)
    existing = _existing_batch(session, tenant_id, idempotency_key)
    if existing:
        operation = _batch_operation(session, existing)
        _arm_exact_operation(session, operation, image_sha, approved_by, approval_ref)
        return _result(existing, operation, image_sha)
    readiness = require_migration_readiness(session, require_mode=False)
    account, code_source, source = _provision_inputs(session, tenant_id, account_id)
    app = _standby_app(session, readiness.standby_assignment.developer_app_id)
    _require_three_apps(session, account_id, code_source, app.id)
    batch, item = _create_batch_item(
        session,
        account,
        source=source,
        idempotency_key=idempotency_key,
        requested_by=requested_by,
        approved_by=approved_by,
        approval_ref=approval_ref,
    )
    operation = _create_operation(
        session,
        batch,
        item,
        account=account,
        source=source,
        code_source=code_source,
        app=app,
        readiness=readiness,
    )
    _audit(session, batch, operation, approved_by)
    session.commit()
    _arm_exact_operation(session, operation, image_sha, approved_by, approval_ref)
    return _result(batch, operation, image_sha)


def _provision_inputs(session, tenant_id: int, account_id: int):
    account = session.get(TgAccount, account_id)
    if not account or account.tenant_id != tenant_id or account.deleted_at is not None:
        raise AuthorizationDrError("account_not_found", f"Account {account_id} is unavailable")
    code_source = require_primary_code_source(account)
    current = list(session.scalars(select(TgAccountAuthorization).where(
        TgAccountAuthorization.account_id == account_id,
        TgAccountAuthorization.logical_slot == "standby_2",
        TgAccountAuthorization.is_slot_current.is_(True),
        TgAccountAuthorization.disabled_at.is_(None),
    )))
    if len(current) > 1:
        raise AuthorizationDrError("migration_source_standby_not_unique", "C current slot is not unique")
    if current and _qualified_my(current[0]):
        raise AuthorizationDrError("standby_2_already_ready", "Qualified C must not be re-provisioned")
    return account, code_source, current[0] if current else None


def _qualified_my(row) -> bool:
    return bool(row.provision_region_code == "my" and row.health_status == "healthy")


def _standby_app(session, app_id: int):
    app = session.get(TelegramDeveloperApp, app_id)
    if not app or not app.is_active:
        raise AuthorizationDrError("developer_app_slot_assignment_conflict", "App C is unavailable")
    return app


def _require_three_apps(session, account_id: int, primary, app_c_id: int) -> None:
    standby = session.scalar(select(TgAccountAuthorization).where(
        TgAccountAuthorization.account_id == account_id,
        TgAccountAuthorization.id != primary.id,
        TgAccountAuthorization.logical_slot == (
            "primary" if primary.logical_slot == "standby_1" else "standby_1"
        ),
        TgAccountAuthorization.is_slot_current.is_(True),
        TgAccountAuthorization.is_current.is_(False),
        TgAccountAuthorization.health_status == "healthy",
        TgAccountAuthorization.disabled_at.is_(None),
    ))
    app_ids = {primary.developer_app_id, standby.developer_app_id if standby else None, app_c_id}
    if standby is None or None in app_ids or len(app_ids) != 3:
        raise AuthorizationDrError("sv_redundancy_incomplete", "Healthy independent B is required before C")


def _create_batch_item(session, account, *, source, idempotency_key, requested_by, approved_by, approval_ref):
    generation = _target_generation(session, account.id, source)
    fingerprint = _fingerprint(account.id, source, generation)
    batch = TgAuthorizationDrBatch(
        tenant_id=account.tenant_id,
        idempotency_key=idempotency_key,
        target_set_fingerprint=fingerprint,
        target_count=1,
        status="approved",
        requested_by=requested_by,
        approved_by=approved_by,
        approval_ref=approval_ref,
        approved_at=_now(),
    )
    session.add(batch)
    session.flush()
    item = TgAuthorizationDrBatchItem(
        batch_id=batch.id,
        tenant_id=account.tenant_id,
        account_id=account.id,
        ordinal=1,
        expected_source_authorization_id=source.id if source else None,
        expected_source_fact_version=source.fact_version if source else 0,
        expected_source_generation=source.slot_generation if source else 0,
        target_generation=generation,
        status="pending",
    )
    session.add(item)
    session.flush()
    return batch, item


def _create_operation(session, batch, item, *, account, source, code_source, app, readiness):
    operation = TgAuthorizationDrOperation(
        tenant_id=batch.tenant_id,
        account_id=item.account_id,
        batch_item_id=item.id,
        operation_type="provision_standby_2",
        logical_slot="standby_2",
        source_authorization_id=source.id if source else None,
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
        request_fingerprint=_operation_fingerprint(item, account, code_source, readiness),
        status="pending",
        requested_by=batch.requested_by,
        approved_by=batch.approved_by,
        approval_ref=batch.approval_ref,
    )
    session.add(operation)
    session.flush()
    item.operation_id = operation.id
    return operation


def _target_generation(session, account_id: int, source) -> int:
    maximum = session.scalar(select(func.max(TgAccountAuthorization.slot_generation)).where(
        TgAccountAuthorization.account_id == account_id,
        TgAccountAuthorization.logical_slot == "standby_2",
    ))
    return max(int(maximum or 0), int(source.slot_generation if source else 0)) + 1


def _operation_fingerprint(item, account, code_source, readiness) -> str:
    return _hash({
        "item": item.id,
        "source": item.expected_source_authorization_id,
        "target_generation": item.target_generation,
        "current": [account.current_authorization_id, account.authorization_generation,
                    account.authorization_fact_generation, account.connection_generation],
        "code_source": [code_source.id, code_source.fact_version],
        "assignment": readiness.assignment_version,
        "egress": [readiness.egress.id, readiness.egress.version],
    })


def _fingerprint(account_id: int, source, generation: int) -> str:
    return _hash({
        "account_id": account_id,
        "source": [source.id, source.fact_version, source.slot_generation] if source else None,
        "target_generation": generation,
    })


def _hash(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _existing_batch(session, tenant_id: int, key: str):
    return session.scalar(select(TgAuthorizationDrBatch).where(
        TgAuthorizationDrBatch.tenant_id == tenant_id,
        TgAuthorizationDrBatch.idempotency_key == key.strip(),
    ))


def _batch_operation(session, batch):
    item = session.scalar(select(TgAuthorizationDrBatchItem).where(
        TgAuthorizationDrBatchItem.batch_id == batch.id,
    ))
    operation = session.get(TgAuthorizationDrOperation, item.operation_id) if item else None
    if not operation or operation.operation_type != "provision_standby_2":
        raise AuthorizationDrError("migration_operation_not_found", "C provision operation is missing")
    return operation


def _result(batch, operation, image_sha: str) -> dict:
    return {
        "batch_id": batch.id,
        "operation_id": operation.id,
        "account_id": operation.account_id,
        "status": operation.status,
        "runtime_mode": "migrate",
        "runtime_image_sha": image_sha,
    }


def _require_approval(requested_by: str, approved_by: str, approval_ref: str) -> None:
    if not all(value.strip() for value in (requested_by, approved_by, approval_ref)):
        raise AuthorizationDrError("approval_ref_required", "Provision approval is incomplete")
    if requested_by.strip() == approved_by.strip():
        raise AuthorizationDrError("approval_actor_conflict", "Approver must differ from requester")


def _audit(session, batch, operation, actor: str) -> None:
    audit(
        session,
        tenant_id=batch.tenant_id,
        actor=actor,
        action="批准缺失 C 的 MY 新建",
        target_type="tg_authorization_dr_operation",
        target_id=operation.id,
        detail=f"approval_ref={batch.approval_ref}; account_id={operation.account_id}",
    )


__all__ = ["prepare_scoped_c_provision"]

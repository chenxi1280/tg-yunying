from __future__ import annotations

import re

from sqlalchemy import select

from app.models import (
    AuthorizationDrRuntimeContract,
    TgAuthorizationDrBatch,
    TgAuthorizationDrBatchItem,
    TgAuthorizationDrOperation,
)
from app.services._common import _now, audit

from .contracts import AuthorizationDrError
from .migration import approve_migration_batch, preview_migration_batch
from .readiness import ABC_CAPABILITY_VERSION, require_migration_readiness


def prepare_scoped_c_migration(
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
    image_sha = _valid_image_sha(runtime_image_sha)
    batch = preview_migration_batch(
        session,
        tenant_id,
        [account_id],
        idempotency_key=idempotency_key,
        actor=requested_by,
    )
    if batch.status == "previewed":
        batch = approve_migration_batch(
            session,
            batch.id,
            expected_version=batch.version,
            approval_ref=approval_ref,
            actor=approved_by,
        )
    operation = _batch_operation(session, batch)
    _arm_exact_operation(session, operation, image_sha, approved_by, approval_ref)
    return {
        "batch_id": batch.id,
        "operation_id": operation.id,
        "account_id": account_id,
        "status": operation.status,
        "runtime_mode": "migrate",
        "runtime_image_sha": image_sha,
    }


def abc_canary_status(session, tenant_id: int, account_id: int) -> dict:
    operations = list(session.scalars(select(TgAuthorizationDrOperation).where(
        TgAuthorizationDrOperation.tenant_id == tenant_id,
        TgAuthorizationDrOperation.account_id == account_id,
        TgAuthorizationDrOperation.operation_type.in_(("provision_standby_1", "migrate_standby_2")),
    ).order_by(TgAuthorizationDrOperation.created_at)))
    contract = session.get(AuthorizationDrRuntimeContract, 1)
    return {
        "tenant_id": tenant_id,
        "account_id": account_id,
        "runtime_mode": contract.mode if contract else "missing",
        "claim_scope_operation_id": contract.claim_scope_operation_id if contract else "",
        "operations": [
            {
                "id": operation.id,
                "type": operation.operation_type,
                "status": operation.status,
                "blocker_code": operation.blocker_code,
                "candidate_authorization_id": operation.candidate_authorization_id,
            }
            for operation in operations
        ],
    }


def _batch_operation(session, batch: TgAuthorizationDrBatch) -> TgAuthorizationDrOperation:
    item = session.scalar(select(TgAuthorizationDrBatchItem).where(
        TgAuthorizationDrBatchItem.batch_id == batch.id,
    ))
    operation = session.get(TgAuthorizationDrOperation, item.operation_id) if item else None
    if not operation:
        raise AuthorizationDrError("migration_operation_not_found", "C migration operation is missing")
    return operation


def _arm_exact_operation(session, operation, image_sha: str, actor: str, approval_ref: str) -> None:
    contract = session.scalar(select(AuthorizationDrRuntimeContract).where(
        AuthorizationDrRuntimeContract.id == 1,
    ).with_for_update())
    if not contract:
        raise AuthorizationDrError("runtime_capability_unproven", "DR runtime contract is missing")
    same_scope = contract.mode == "migrate" and contract.claim_scope_operation_id == operation.id
    if contract.mode != "off" and not same_scope:
        raise AuthorizationDrError("runtime_capability_unproven", "DR runtime is not safely off")
    contract.required_node_capability_version = ABC_CAPABILITY_VERSION
    contract.required_node_runtime_image_sha = image_sha
    contract.claim_scope_operation_id = operation.id
    contract.mode = "migrate"
    contract.contract_epoch += 1
    contract.version += 1
    contract.updated_by = actor
    contract.updated_at = _now()
    session.flush()
    require_migration_readiness(session)
    audit(
        session,
        tenant_id=operation.tenant_id,
        actor=actor,
        action="仅开放单个 C 备份 operation",
        target_type="tg_authorization_dr_operation",
        target_id=operation.id,
        detail=f"approval_ref={approval_ref}; runtime_image_sha={image_sha}",
    )
    session.commit()


def _valid_image_sha(value: str) -> str:
    normalized = value.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", normalized):
        raise AuthorizationDrError("runtime_image_mismatch", "Exact runtime image SHA is required")
    return normalized


__all__ = ["abc_canary_status", "prepare_scoped_c_migration"]

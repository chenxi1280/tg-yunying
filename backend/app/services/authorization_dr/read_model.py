from __future__ import annotations

from collections import Counter

from sqlalchemy import select

from app.models import (
    AuthorizationDrExecutionNode,
    TgAuthorizationDrBatch,
    TgAuthorizationDrBatchItem,
    TgAuthorizationDrOperation,
)

from .contracts import AuthorizationDrError


def migration_batch_out(session, batch_id: str, tenant_id: int) -> dict:
    batch = session.get(TgAuthorizationDrBatch, batch_id)
    if not batch or batch.tenant_id != tenant_id:
        raise AuthorizationDrError("migration_batch_not_found", "Migration batch does not exist")
    items = list(session.scalars(select(TgAuthorizationDrBatchItem).where(
        TgAuthorizationDrBatchItem.batch_id == batch.id,
    ).order_by(TgAuthorizationDrBatchItem.ordinal)))
    return {
        "id": batch.id,
        "tenant_id": batch.tenant_id,
        "operation_type": batch.operation_type,
        "target_set_fingerprint": batch.target_set_fingerprint,
        "target_count": batch.target_count,
        "status": batch.status,
        "version": batch.version,
        "requested_by": batch.requested_by,
        "approval_ref": batch.approval_ref,
        "approved_by": batch.approved_by,
        "approved_at": batch.approved_at,
        "created_at": batch.created_at,
        "execution_finished_at": batch.execution_finished_at,
        "finished_at": batch.finished_at,
        "status_counts": dict(Counter(item.status for item in items)),
        "items": [_batch_item_out(item) for item in items],
    }


def operation_out(session, operation_id: str, tenant_id: int) -> dict:
    operation = session.get(TgAuthorizationDrOperation, operation_id)
    if not operation or operation.tenant_id != tenant_id:
        raise AuthorizationDrError("migration_operation_not_found", "Migration operation does not exist")
    fields = (
        "id", "tenant_id", "account_id", "operation_type", "logical_slot",
        "source_authorization_id", "candidate_authorization_id", "source_generation", "target_generation",
        "developer_app_id", "developer_app_api_id_snapshot", "assignment_version", "egress_id", "egress_version",
        "status", "blocker_code", "operation_version", "execution_generation", "owner_node_id", "owner_epoch",
        "remote_effect_started_at", "remote_call_state", "reconcile_case_id", "reconcile_status",
        "reconciled_at", "requested_by", "approved_by", "approval_ref",
        "created_at", "updated_at", "finished_at",
    )
    return {field: getattr(operation, field) for field in fields}


def list_execution_nodes(session) -> list[AuthorizationDrExecutionNode]:
    return list(session.scalars(select(AuthorizationDrExecutionNode).order_by(
        AuthorizationDrExecutionNode.region_code,
        AuthorizationDrExecutionNode.id,
    )))


def _batch_item_out(item) -> dict:
    fields = (
        "id", "account_id", "ordinal", "expected_source_authorization_id",
        "expected_source_fact_version", "expected_source_generation", "target_generation",
        "status", "outcome", "blocker_code", "operation_id",
    )
    return {field: getattr(item, field) for field in fields}


__all__ = ["list_execution_nodes", "migration_batch_out", "operation_out"]

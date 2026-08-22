from __future__ import annotations

from sqlalchemy import select

from app.models import (
    TgAuthorizationDrBatch,
    TgAuthorizationDrBatchItem,
    TgAuthorizationDrOperation,
)


def online_abc_item_operations(session, batch, item) -> dict:
    keys = online_abc_operation_keys(batch, item)
    return {
        "b": _operation_by_key(session, batch.tenant_id, keys["b"]),
        "c": _c_operation_by_batch_key(session, batch.tenant_id, item.account_id, keys["c"]),
        "e4": _operation_by_key(session, batch.tenant_id, keys["e4"]),
    }


def online_abc_operation_keys(batch, item) -> dict[str, str]:
    base = f"online-abc:{batch.id}:{item.ordinal}"
    return {"b": f"{base}:b", "c": f"{base}:b:c", "e4": f"{base}:e4"}


def _operation_by_key(session, tenant_id: int, key: str):
    return session.scalar(select(TgAuthorizationDrOperation).where(
        TgAuthorizationDrOperation.tenant_id == tenant_id,
        TgAuthorizationDrOperation.idempotency_key == key,
    ))


def _c_operation_by_batch_key(session, tenant_id: int, account_id: int, key: str):
    migration_batch = session.scalar(select(TgAuthorizationDrBatch).where(
        TgAuthorizationDrBatch.tenant_id == tenant_id,
        TgAuthorizationDrBatch.idempotency_key == key,
    ))
    if not migration_batch:
        return None
    item = session.scalar(select(TgAuthorizationDrBatchItem).where(
        TgAuthorizationDrBatchItem.batch_id == migration_batch.id,
        TgAuthorizationDrBatchItem.account_id == account_id,
    ))
    return session.get(TgAuthorizationDrOperation, item.operation_id) if item and item.operation_id else None


__all__ = ["online_abc_item_operations", "online_abc_operation_keys"]

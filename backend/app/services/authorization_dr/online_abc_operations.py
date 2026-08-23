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
        "c": _latest_c_operation(session, batch.tenant_id, item.account_id, keys["c"]),
        "e4": _latest_e4_operation(session, batch.tenant_id, keys["e4"]),
    }


def online_abc_operation_keys(batch, item) -> dict[str, str]:
    base = f"online-abc:{batch.id}:{item.ordinal}"
    return {"b": f"{base}:b", "c": f"{base}:b:c", "e4": f"{base}:e4"}


def next_online_abc_c_key(session, batch, item) -> str:
    base = online_abc_operation_keys(batch, item)["c"]
    count = len(_c_batches(session, batch.tenant_id, base))
    return base if count == 0 else f"{base}:retry:{count}"


def next_online_abc_e4_key(session, batch, item) -> str:
    base = online_abc_operation_keys(batch, item)["e4"]
    count = len(_e4_operations(session, batch.tenant_id, base))
    return base if count == 0 else f"{base}:retry:{count}"


def _operation_by_key(session, tenant_id: int, key: str):
    return session.scalar(select(TgAuthorizationDrOperation).where(
        TgAuthorizationDrOperation.tenant_id == tenant_id,
        TgAuthorizationDrOperation.idempotency_key == key,
    ))


def _latest_c_operation(session, tenant_id: int, account_id: int, key: str):
    batches = _c_batches(session, tenant_id, key)
    migration_batch = batches[-1] if batches else None
    if not migration_batch:
        return None
    item = session.scalar(select(TgAuthorizationDrBatchItem).where(
        TgAuthorizationDrBatchItem.batch_id == migration_batch.id,
        TgAuthorizationDrBatchItem.account_id == account_id,
    ))
    return session.get(TgAuthorizationDrOperation, item.operation_id) if item and item.operation_id else None


def _latest_e4_operation(session, tenant_id: int, key: str):
    operations = _e4_operations(session, tenant_id, key)
    return operations[-1] if operations else None


def _e4_operations(session, tenant_id: int, key: str):
    return list(session.scalars(select(TgAuthorizationDrOperation).where(
        TgAuthorizationDrOperation.tenant_id == tenant_id,
        TgAuthorizationDrOperation.idempotency_key.like(f"{key}%"),
        TgAuthorizationDrOperation.operation_type == "abc_e4_primary_send",
    ).order_by(TgAuthorizationDrOperation.created_at, TgAuthorizationDrOperation.id)))


def _c_batches(session, tenant_id: int, key: str):
    return list(session.scalars(select(TgAuthorizationDrBatch).where(
        TgAuthorizationDrBatch.tenant_id == tenant_id,
        TgAuthorizationDrBatch.idempotency_key.like(f"{key}%"),
    ).order_by(TgAuthorizationDrBatch.created_at, TgAuthorizationDrBatch.id)))


__all__ = [
    "next_online_abc_c_key",
    "next_online_abc_e4_key",
    "online_abc_item_operations",
    "online_abc_operation_keys",
]

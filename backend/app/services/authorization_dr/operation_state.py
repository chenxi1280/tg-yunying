from __future__ import annotations

from sqlalchemy import select

from app.models import TgAuthorizationDrBatchItem, TgAuthorizationDrOperation
from app.services._common import _now

from .contracts import AuthorizationDrError


def owned_operation(
    session,
    operation_id: str,
    *,
    node_id: str,
    owner_epoch: int,
    lease_token: str,
) -> TgAuthorizationDrOperation:
    operation = session.scalar(select(TgAuthorizationDrOperation).where(
        TgAuthorizationDrOperation.id == operation_id,
    ).with_for_update())
    if not operation or operation.owner_node_id != node_id or operation.owner_epoch != owner_epoch:
        raise AuthorizationDrError("execution_node_mismatch", "Operation owner changed")
    valid_lease = (
        operation.lease_token == lease_token
        and operation.lease_expires_at
        and operation.lease_expires_at > _now()
    )
    if not valid_lease:
        raise AuthorizationDrError("malaysia_owner_fencing_unproven", "Operation lease is stale")
    return operation


def mark_item(session, operation, status: str, *, blocker: str = "") -> None:
    item = session.get(TgAuthorizationDrBatchItem, operation.batch_item_id)
    if item:
        item.status = status
        item.blocker_code = blocker
        item.version += 1


__all__ = ["mark_item", "owned_operation"]

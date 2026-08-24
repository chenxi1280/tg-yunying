from __future__ import annotations

from collections import Counter

from sqlalchemy import func, select

from app.models import (
    AuditLog,
    AuthorizationDrExecutionNode,
    AuthorizationDrRuntimeContract,
    TgAuthorizationDrOperation,
    TgAuthorizationOnlineAbcBatch,
    TgAuthorizationOnlineAbcItem,
)
from app.services._common import audit

from .contracts import AuthorizationDrError
from .online_abc_manifest import ACTIVE_OPERATION_STATUSES


MAX_CHUNK_ACCOUNTS = 10
CHUNK_PAUSE_ACTION = "ABC runner chunk 边界停批"
CHUNK_RESUME_ACTION = "恢复 ABC runner chunk 边界"
QUIESCENT_ITEM_STATUSES = {"pending", "succeeded"}


def require_chunk_size(max_accounts: int) -> None:
    if max_accounts < 1 or max_accounts > MAX_CHUNK_ACCOUNTS:
        raise AuthorizationDrError("online_abc_chunk_size_invalid", "Chunk size must be between 1 and 10")


def resume_online_abc_chunk(session, batch_id: str, *, actor: str, approval_ref: str) -> bool:
    batch = _locked_batch(session, batch_id)
    if batch.status != "stopped" or not _latest_chunk_pause(session, batch, approval_ref):
        return False
    counts = _require_quiescent_boundary(session, batch)
    batch.status = "running"
    batch.version += 1
    _audit_chunk(
        session,
        batch,
        action=CHUNK_RESUME_ACTION,
        actor=actor,
        approval_ref=approval_ref,
        counts=counts,
        processed_count=0,
    )
    session.commit()
    return True


def pause_online_abc_chunk(
    session, batch_id: str, *, actor: str, approval_ref: str, processed_count: int,
) -> bool:
    batch = _locked_batch(session, batch_id)
    if batch.status != "running":
        return False
    counts = _require_quiescent_boundary(session, batch)
    batch.status = "stopped"
    batch.version += 1
    _audit_chunk(
        session,
        batch,
        action=CHUNK_PAUSE_ACTION,
        actor=actor,
        approval_ref=approval_ref,
        counts=counts,
        processed_count=processed_count,
    )
    session.commit()
    return True


def chunk_result(view: dict, account_ids: list[int], max_accounts: int) -> dict:
    return {
        **view,
        "chunk": {
            "max_accounts": max_accounts,
            "processed_count": len(account_ids),
            "account_ids": list(account_ids),
        },
    }


def require_item_runnable(item) -> None:
    if "blocked" in {item.standby_1_plan, item.standby_2_plan}:
        raise AuthorizationDrError("online_abc_item_blocked", "Frozen account requires repair before login")


def require_slot_ready(plan: str, operation, code: str) -> None:
    if plan == "already_qualified":
        return
    if operation is None or operation.status != "succeeded":
        status = operation.status if operation else "missing"
        raise AuthorizationDrError(code, f"Operation is {status}")


def _locked_batch(session, batch_id: str):
    batch = session.scalar(select(TgAuthorizationOnlineAbcBatch).where(
        TgAuthorizationOnlineAbcBatch.id == batch_id,
    ).with_for_update().execution_options(populate_existing=True))
    if not batch:
        raise AuthorizationDrError("online_abc_batch_not_found", "Online ABC batch is unavailable")
    return batch


def _require_quiescent_boundary(session, batch) -> Counter:
    items = list(session.scalars(select(TgAuthorizationOnlineAbcItem).where(
        TgAuthorizationOnlineAbcItem.batch_id == batch.id,
    )))
    counts = Counter(item.status for item in items)
    valid = len(items) == batch.target_count and bool(counts["pending"])
    if not valid or set(counts) - QUIESCENT_ITEM_STATUSES:
        raise AuthorizationDrError("online_abc_chunk_not_quiescent", "Chunk item boundary changed")
    _require_global_boundary(session)
    return counts


def _require_global_boundary(session) -> None:
    runtime = session.get(AuthorizationDrRuntimeContract, 1)
    if not runtime or runtime.mode != "off" or runtime.claim_scope_operation_id:
        raise AuthorizationDrError("runtime_capability_unproven", "DR runtime must be safely off")
    sensitive = session.scalar(select(TgAuthorizationDrOperation.id).where(
        TgAuthorizationDrOperation.status.in_(ACTIVE_OPERATION_STATUSES),
    ).limit(1))
    if sensitive:
        raise AuthorizationDrError("online_abc_chunk_sensitive_operation", "Sensitive operation is active")
    my_clients = session.scalar(select(func.coalesce(func.sum(
        AuthorizationDrExecutionNode.active_client_count
    ), 0)).where(AuthorizationDrExecutionNode.region_code == "my"))
    if my_clients:
        raise AuthorizationDrError("malaysia_client_leak", "Malaysia active client count must be zero")


def _latest_chunk_pause(session, batch, approval_ref: str) -> bool:
    row = session.scalar(select(AuditLog).where(
        AuditLog.target_type == "tg_authorization_online_abc_batches",
        AuditLog.target_id == batch.id,
    ).order_by(AuditLog.id.desc()).limit(1))
    return bool(
        row and row.action == CHUNK_PAUSE_ACTION
        and f"approval_ref={approval_ref};" in row.detail
        and f"execution_release={batch.execution_release_sha or batch.deployed_release_sha};" in row.detail
    )


def _audit_chunk(
    session, batch, *, action, actor, approval_ref, counts, processed_count,
) -> None:
    audit(
        session,
        tenant_id=batch.tenant_id,
        actor=actor,
        action=action,
        target_type="tg_authorization_online_abc_batches",
        target_id=batch.id,
        detail=(
            f"approval_ref={approval_ref}; execution_release="
            f"{batch.execution_release_sha or batch.deployed_release_sha}; "
            f"processed_count={processed_count}; succeeded={counts['succeeded']}; "
            f"pending={counts['pending']}"
        ),
    )


__all__ = [
    "CHUNK_PAUSE_ACTION", "MAX_CHUNK_ACCOUNTS", "chunk_result",
    "pause_online_abc_chunk", "require_chunk_size", "require_item_runnable",
    "require_slot_ready", "resume_online_abc_chunk",
]

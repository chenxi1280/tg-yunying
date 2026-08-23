from __future__ import annotations

from sqlalchemy import func, select

from app.models import (
    AuditLog,
    AuthorizationDrExecutionNode,
    TgAuthorizationOnlineAbcBatch,
    TgAuthorizationOnlineAbcItem,
)
from app.services._common import audit

from .contracts import AuthorizationDrError
from .online_abc import _require_no_global_unknown, _require_runtime_off
from .online_abc_operations import online_abc_item_operations
from .online_abc_primary import stop_completed_primary_drift
from .online_abc_read import render_online_abc_status


def accept_online_abc_observation(
    session,
    batch_id: str,
    *,
    actor: str,
    approval_ref: str,
) -> dict:
    _require_actor(actor, approval_ref)
    batch = _locked_batch(session, batch_id)
    if batch.status == "accepted":
        return render_online_abc_status(session, batch.id)
    _require_observing(batch)
    _require_runtime_off(session)
    _require_no_global_unknown(session)
    _require_my_clients_zero(session)
    items = _successful_items(session, batch)
    if stop_completed_primary_drift(session, batch, actor=actor, approval_ref=approval_ref):
        raise AuthorizationDrError("online_abc_primary_drift", "A drifted during observation")
    _require_primary_send_proven(session, batch, items)
    batch.status = "accepted"
    batch.version += 1
    audit(
        session,
        tenant_id=batch.tenant_id,
        actor=actor,
        action="接受 10 账号 ABC A 与发送验收",
        target_type="tg_authorization_online_abc_batches",
        target_id=batch.id,
        detail=(
            f"approval_ref={approval_ref}; target_count={batch.target_count}; "
            "gate=primary_stable_and_saved_message_remote_id"
        ),
    )
    session.commit()
    return render_online_abc_status(session, batch.id)


def _locked_batch(session, batch_id: str):
    batch = session.scalar(select(TgAuthorizationOnlineAbcBatch).where(
        TgAuthorizationOnlineAbcBatch.id == batch_id,
    ).with_for_update())
    if not batch:
        raise AuthorizationDrError("online_abc_batch_not_found", "Online ABC batch is unavailable")
    return batch


def _require_observing(batch) -> None:
    if batch.status != "observing":
        raise AuthorizationDrError("online_abc_observation_not_open", f"Batch is {batch.status}")


def _successful_items(session, batch) -> list:
    items = list(session.scalars(select(TgAuthorizationOnlineAbcItem).where(
        TgAuthorizationOnlineAbcItem.batch_id == batch.id,
    )))
    incomplete = sum(item.outcome != "succeeded" for item in items)
    if incomplete or len(items) != batch.target_count:
        raise AuthorizationDrError("online_abc_observation_failed", "Canary items are not all succeeded")
    return items


def _require_primary_send_proven(session, batch, items: list) -> None:
    missing = []
    for item in items:
        operation = online_abc_item_operations(session, batch, item)["e4"]
        detail = _send_audit_detail(session, operation.id) if operation else ""
        remote_id = _primary_remote_id(detail)
        if not operation or operation.status != "succeeded" or not remote_id:
            missing.append(item.account_id)
    if missing:
        raise AuthorizationDrError(
            "online_abc_primary_send_unproven",
            f"A Saved Messages remote fact is missing for accounts={','.join(map(str, missing))}",
        )


def _send_audit_detail(session, operation_id: str) -> str:
    return session.scalar(select(AuditLog.detail).where(
        AuditLog.target_type == "tg_authorization_dr_operation",
        AuditLog.target_id == operation_id,
        AuditLog.action == "完成 ABC canary E4",
    ).order_by(AuditLog.id.desc()).limit(1)) or ""


def _primary_remote_id(detail: str) -> str:
    marker = "primary_saved_message_id="
    if marker not in detail:
        return ""
    return detail.split(marker, 1)[1].split(";", 1)[0].strip()


def _require_my_clients_zero(session) -> None:
    active = session.scalar(select(func.sum(AuthorizationDrExecutionNode.active_client_count)).where(
        AuthorizationDrExecutionNode.region_code == "my",
    ))
    if int(active or 0) != 0:
        raise AuthorizationDrError("malaysia_owner_fencing_unproven", "MY node has active clients")


def _require_actor(actor: str, approval_ref: str) -> None:
    if not actor.strip() or not approval_ref.strip():
        raise AuthorizationDrError("approval_ref_required", "Actor and approval ref are required")


__all__ = ["accept_online_abc_observation"]

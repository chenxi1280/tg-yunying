from __future__ import annotations

from sqlalchemy import func, select

from app.models import (
    AuthorizationDrExecutionNode,
    TgAuthorizationOnlineAbcBatch,
    TgAuthorizationOnlineAbcItem,
)
from app.services._common import _now, audit

from .contracts import AuthorizationDrError
from .online_abc import _require_no_global_unknown, _require_runtime_off
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
    _require_observation_elapsed(batch)
    _require_all_items_succeeded(session, batch)
    if stop_completed_primary_drift(session, batch, actor=actor, approval_ref=approval_ref):
        raise AuthorizationDrError("online_abc_primary_drift", "A drifted during observation")
    batch.status = "accepted"
    batch.version += 1
    audit(
        session,
        tenant_id=batch.tenant_id,
        actor=actor,
        action="接受 10 账号 ABC 观察窗",
        target_type="tg_authorization_online_abc_batches",
        target_id=batch.id,
        detail=f"approval_ref={approval_ref}; target_count={batch.target_count}",
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


def _require_observation_elapsed(batch) -> None:
    if not batch.observation_closes_at or _now() < batch.observation_closes_at:
        raise AuthorizationDrError(
            "online_abc_canary_observation_incomplete",
            "The mandatory observation window has not closed",
        )


def _require_all_items_succeeded(session, batch) -> None:
    incomplete = session.scalar(select(func.count()).select_from(TgAuthorizationOnlineAbcItem).where(
        TgAuthorizationOnlineAbcItem.batch_id == batch.id,
        TgAuthorizationOnlineAbcItem.outcome != "succeeded",
    ))
    if incomplete:
        raise AuthorizationDrError("online_abc_observation_failed", "Canary items are not all succeeded")


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

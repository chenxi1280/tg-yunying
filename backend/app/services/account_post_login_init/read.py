from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    TgAccountFullInitialization,
    TgAccountLoginBatchItem,
    TgPostLoginAbcRequest,
)


def post_login_initialization_detail(
    session: Session,
    tenant_id: int,
    *,
    batch_id: int,
    item_id: int,
) -> dict:
    item = session.get(TgAccountLoginBatchItem, item_id)
    if not item or item.tenant_id != tenant_id or item.batch_id != batch_id:
        raise ValueError("batch login item not found")
    owner = session.get(TgAccountFullInitialization, item.post_initialization_id)
    if not owner or owner.tenant_id != tenant_id or owner.account_id != item.account_id:
        raise ValueError("post-login initialization not found")
    request = session.scalar(
        select(TgPostLoginAbcRequest).where(
            TgPostLoginAbcRequest.full_initialization_id == owner.id
        )
    )
    return post_login_initialization_out(session, owner, request=request)


def post_login_initialization_out(session, owner, *, request=None) -> dict:
    request = request or session.scalar(
        select(TgPostLoginAbcRequest).where(
            TgPostLoginAbcRequest.full_initialization_id == owner.id
        )
    )
    return {
        "id": owner.id,
        "account_id": owner.account_id,
        "generation": owner.generation,
        "predecessor_initialization_id": owner.predecessor_initialization_id,
        "target_pool_id": owner.target_pool_id,
        "policy_version": owner.policy_version,
        "status": owner.status,
        "stage": owner.stage,
        "source_two_fa_kind": owner.source_two_fa_kind,
        "two_fa_status": owner.two_fa_status,
        "two_fa_call_state": owner.two_fa_call_state,
        "two_fa_evidence_present": bool(owner.two_fa_evidence_ref),
        "profile_status": owner.profile_status,
        "profile_batch_id": owner.profile_batch_id,
        "profile_action_types": json.loads(owner.profile_action_types or "[]"),
        "profile_evidence_present": bool(owner.profile_evidence_ref),
        "abc_status": owner.abc_status,
        "abc_batch_id": owner.abc_batch_id,
        "abc_evidence_present": bool(owner.abc_evidence_ref),
        "abc_request_id": request.id if request else None,
        "abc_request_status": request.status if request else "not_created",
        "failure_type": owner.failure_type,
        "failure_detail": owner.failure_detail,
        "execution_owner": owner.execution_owner,
        "version": owner.version,
        "next_retry_at": owner.next_retry_at,
        "started_at": owner.started_at,
        "finished_at": owner.finished_at,
        "created_at": owner.created_at,
        "updated_at": owner.updated_at,
    }


__all__ = ["post_login_initialization_detail", "post_login_initialization_out"]

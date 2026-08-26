from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Tenant,
    TgAccount,
    TgAccountFullInitialization,
    TgAccountLoginBatchItem,
    TgAccountLoginPostInitializationBinding,
    TgAuthorizationDrOperation,
    TgAuthorizationOnlineAbcBatch,
    TgAuthorizationOnlineAbcItem,
    TgPostLoginAbcRequest,
)
from app.services._common import _now, audit
from app.services.authorization_dr import (
    apply_post_login_online_abc_batch,
    completed_abc_evidence_ref,
    preview_post_login_online_abc_batch,
)
from app.services.authorization_dr.online_abc import OPEN_BATCH_STATUSES

from .contracts import FullInitializationClaim


ABC_POLL_SECONDS = 10
TERMINAL_OPERATION_STATUSES = {
    "succeeded",
    "failed",
    "manual_required",
    "migration_rolled_back_forward",
}


def execute_abc_stage(session_factory, claim: FullInitializationClaim) -> None:
    with session_factory() as session:
        owner = _load_claim(session, claim)
        _validate_account_fence(session, owner)
        existing_evidence = completed_abc_evidence_ref(
            session,
            owner.tenant_id,
            owner.account_id,
        )
        if existing_evidence:
            _finish_abc_success(session, owner, existing_evidence)
            session.commit()
            return
        item = _linked_online_item(session, owner)
        item = item or _active_online_item(session, owner.account_id)
        if item:
            _sync_from_online_item(session, owner, item)
            session.commit()
            return
        if _has_active_account_operation(session, owner.account_id):
            _wait_for_abc(owner, "waiting_account_owner")
            session.commit()
            return
        request = _abc_request(session, owner.id)
        if request:
            _sync_from_request(owner, request)
            session.commit()
            return
        _create_abc_request(session, owner)
        session.commit()


def _create_abc_request(session, owner) -> None:
    session.add(TgPostLoginAbcRequest(
        tenant_id=owner.tenant_id,
        account_id=owner.account_id,
        full_initialization_id=owner.id,
        requested_by=owner.originating_actor,
    ))
    owner.status = "waiting_abc_approval"
    owner.abc_status = "waiting_approval"
    owner.lease_token = ""
    owner.lease_expires_at = None
    owner.next_retry_at = None
    owner.version += 1
    audit(
        session,
        tenant_id=owner.tenant_id,
        actor=owner.execution_owner,
        action="创建批量登录单账号 ABC 审批请求",
        target_type="tg_post_login_abc_request",
        target_id=str(owner.id),
        detail=f"account_id={owner.account_id}",
    )


def preview_post_login_abc_request(
    session: Session,
    tenant_id: int,
    request_id: int,
    *,
    deployed_release_sha: str,
) -> dict:
    request = _require_request(session, tenant_id, request_id)
    _require_request_ready(session, request)
    preview = preview_post_login_online_abc_batch(
        session,
        tenant_id,
        request.account_id,
        idempotency_key=_abc_idempotency_key(request),
        deployed_release_sha=deployed_release_sha,
    )
    return {
        "request_id": request.id,
        "request_version": request.request_version,
        "account_id": request.account_id,
        "deployed_release_sha": preview["deployed_release_sha"],
        "fingerprint": preview["fingerprint"],
        "classification_counts": preview.get("classification_counts", {}),
    }


def approve_post_login_abc_request(
    session: Session,
    tenant_id: int,
    request_id: int,
    *,
    expected_version: int,
    deployed_release_sha: str,
    expected_fingerprint: str,
    approved_by: str,
    approval_ref: str,
) -> dict:
    request = session.scalar(
        select(TgPostLoginAbcRequest).where(
            TgPostLoginAbcRequest.id == request_id
        ).with_for_update()
    )
    if not request or request.tenant_id != tenant_id:
        raise ValueError("post-login ABC request not found")
    _require_approval(
        request,
        expected_version=expected_version,
        actor=approved_by,
        approval_ref=approval_ref,
    )
    _require_request_ready(session, request)
    result = apply_post_login_online_abc_batch(
        session,
        tenant_id,
        request.account_id,
        idempotency_key=_abc_idempotency_key(request),
        deployed_release_sha=deployed_release_sha,
        expected_fingerprint=expected_fingerprint,
        requested_by=request.requested_by,
        approved_by=approved_by,
        approval_ref=approval_ref,
    )
    request = _record_abc_approval(
        session,
        request_id,
        tenant_id=tenant_id,
        deployed_release_sha=deployed_release_sha,
        expected_fingerprint=expected_fingerprint,
        approved_by=approved_by,
        approval_ref=approval_ref,
        abc_batch_id=result["batch_id"],
    )
    session.commit()
    return post_login_abc_request_out(request)


def _record_abc_approval(
    session: Session,
    request_id: int,
    *,
    tenant_id: int,
    deployed_release_sha: str,
    expected_fingerprint: str,
    approved_by: str,
    approval_ref: str,
    abc_batch_id: str,
) -> TgPostLoginAbcRequest:
    request = session.get(TgPostLoginAbcRequest, request_id)
    owner = session.get(TgAccountFullInitialization, request.full_initialization_id)
    request.status = "approved"
    request.approved_by = approved_by
    request.approval_ref = approval_ref
    request.deployed_release_sha = deployed_release_sha
    request.preview_fingerprint = expected_fingerprint
    request.abc_batch_id = abc_batch_id
    request.approved_at = _now()
    request.request_version += 1
    owner.abc_batch_id = abc_batch_id
    _wait_for_abc(owner, "approved")
    audit(
        session,
        tenant_id=tenant_id,
        actor=approved_by,
        action="批准批量登录单账号 ABC 请求",
        target_type="tg_post_login_abc_request",
        target_id=str(request.id),
        detail=f"approval_ref={approval_ref}; abc_batch_id={abc_batch_id}",
    )
    return request


def post_login_abc_request_out(request: TgPostLoginAbcRequest) -> dict:
    return {
        "id": request.id,
        "tenant_id": request.tenant_id,
        "account_id": request.account_id,
        "full_initialization_id": request.full_initialization_id,
        "status": request.status,
        "request_version": request.request_version,
        "requested_by": request.requested_by,
        "approved_by": request.approved_by,
        "approval_ref": request.approval_ref,
        "deployed_release_sha": request.deployed_release_sha,
        "preview_fingerprint": request.preview_fingerprint,
        "abc_batch_id": request.abc_batch_id,
        "failure_type": request.failure_type,
        "failure_detail": request.failure_detail,
        "created_at": request.created_at,
        "approved_at": request.approved_at,
        "finished_at": request.finished_at,
    }


def list_post_login_abc_requests(
    session: Session,
    tenant_id: int,
    *,
    limit: int,
    batch_id: int | None = None,
) -> list[dict]:
    query = select(TgPostLoginAbcRequest).where(
        TgPostLoginAbcRequest.tenant_id == tenant_id
    )
    if batch_id is not None:
        query = (
            query.join(
                TgAccountLoginPostInitializationBinding,
                TgAccountLoginPostInitializationBinding.full_initialization_id
                == TgPostLoginAbcRequest.full_initialization_id,
            )
            .join(
                TgAccountLoginBatchItem,
                TgAccountLoginBatchItem.id
                == TgAccountLoginPostInitializationBinding.login_item_id,
            )
            .where(TgAccountLoginBatchItem.batch_id == batch_id)
            .distinct()
        )
    rows = session.scalars(query.order_by(TgPostLoginAbcRequest.id.desc()).limit(limit))
    return [post_login_abc_request_out(row) for row in rows]


def _sync_from_online_item(session, owner, item) -> None:
    batch = session.get(TgAuthorizationOnlineAbcBatch, item.batch_id)
    owner.abc_batch_id = item.batch_id
    if item.status == "succeeded" and item.outcome == "succeeded":
        evidence = completed_abc_evidence_ref(
            session,
            owner.tenant_id,
            owner.account_id,
        )
        if evidence:
            _finish_abc_success(session, owner, evidence)
            _finish_request(session, owner.id, "succeeded")
            return
        _finish_abc_terminal(owner, "reconcile_unknown", "abc_success_evidence_unproven")
        _finish_request(session, owner.id, "reconcile_unknown")
        return
    if batch and batch.status in OPEN_BATCH_STATUSES:
        _mark_request_running(session, owner.id)
        _wait_for_abc(owner, "running")
        return
    if item.outcome in {"reconcile_unknown", "deferred_reconcile"}:
        _finish_abc_terminal(owner, "reconcile_unknown", item.outcome)
        _finish_request(session, owner.id, "reconcile_unknown")
        return
    _finish_abc_terminal(owner, "manual_required", item.blocker_code or item.outcome)
    _finish_request(session, owner.id, "manual_required")


def _sync_from_request(owner, request) -> None:
    if request.status == "waiting_approval":
        owner.status = "waiting_abc_approval"
        owner.abc_status = "waiting_approval"
        owner.lease_token = ""
        owner.lease_expires_at = None
        owner.next_retry_at = None
        owner.version += 1
        return
    if request.status in {"approved", "running"}:
        owner.abc_batch_id = request.abc_batch_id
        _wait_for_abc(owner, request.status)
        return
    terminal = "reconcile_unknown" if request.status == "reconcile_unknown" else "manual_required"
    _finish_abc_terminal(owner, terminal, request.failure_type or request.status)


def _validate_account_fence(session, owner) -> None:
    account = session.get(TgAccount, owner.account_id)
    tenant = session.get(Tenant, owner.tenant_id)
    valid = bool(
        account
        and tenant
        and account.tenant_id == owner.tenant_id
        and account.deleted_at is None
        and account.account_identity == "normal"
        and account.authorization_generation == owner.authorization_generation
        and tenant.fixed_two_fa_password_version == owner.fixed_two_fa_version
    )
    if not valid:
        raise RuntimeError("post-login ABC account lifecycle fence changed")


def _linked_online_item(session, owner):
    if not owner.abc_batch_id:
        return None
    return session.scalar(
        select(TgAuthorizationOnlineAbcItem).where(
            TgAuthorizationOnlineAbcItem.batch_id == owner.abc_batch_id,
            TgAuthorizationOnlineAbcItem.account_id == owner.account_id,
        ).limit(1)
    )


def _active_online_item(session, account_id: int):
    return session.scalar(
        select(TgAuthorizationOnlineAbcItem)
        .join(
            TgAuthorizationOnlineAbcBatch,
            TgAuthorizationOnlineAbcBatch.id == TgAuthorizationOnlineAbcItem.batch_id,
        )
        .where(
            TgAuthorizationOnlineAbcItem.account_id == account_id,
            TgAuthorizationOnlineAbcBatch.status.in_(OPEN_BATCH_STATUSES),
        )
        .order_by(TgAuthorizationOnlineAbcBatch.created_at.desc())
        .limit(1)
    )


def _has_active_account_operation(session, account_id: int) -> bool:
    operation = session.scalar(
        select(TgAuthorizationDrOperation.id).where(
            TgAuthorizationDrOperation.account_id == account_id,
            TgAuthorizationDrOperation.status.not_in(TERMINAL_OPERATION_STATUSES),
        ).limit(1)
    )
    return operation is not None


def _finish_abc_success(session, owner, evidence_ref: str) -> None:
    owner.abc_status = "succeeded"
    owner.abc_evidence_ref = evidence_ref
    owner.status = "succeeded"
    owner.stage = "succeeded"
    owner.failure_type = ""
    owner.failure_detail = ""
    owner.finished_at = _now()
    owner.next_retry_at = None
    owner.lease_token = ""
    owner.lease_expires_at = None
    owner.version += 1
    audit(
        session,
        tenant_id=owner.tenant_id,
        actor=owner.execution_owner,
        action="完成批量登录 ABC 初始化",
        target_type="tg_account_full_initialization",
        target_id=str(owner.id),
        detail=evidence_ref,
    )


def _wait_for_abc(owner, status: str) -> None:
    owner.status = "waiting_abc"
    owner.abc_status = status
    owner.next_retry_at = _now() + timedelta(seconds=ABC_POLL_SECONDS)
    owner.lease_token = ""
    owner.lease_expires_at = None
    owner.version += 1


def _finish_abc_terminal(owner, status: str, detail: str) -> None:
    owner.status = status
    owner.stage = status
    owner.abc_status = status
    owner.failure_type = f"abc_{status}"
    owner.failure_detail = detail[:500]
    owner.finished_at = _now()
    owner.next_retry_at = None
    owner.lease_token = ""
    owner.lease_expires_at = None
    owner.version += 1


def _finish_request(session, owner_id: int, status: str) -> None:
    request = _abc_request(session, owner_id)
    if not request:
        return
    request.status = status
    request.finished_at = _now()
    request.request_version += 1


def _mark_request_running(session, owner_id: int) -> None:
    request = _abc_request(session, owner_id)
    if request and request.status == "approved":
        request.status = "running"
        request.request_version += 1


def _abc_request(session, owner_id: int):
    return session.scalar(
        select(TgPostLoginAbcRequest).where(
            TgPostLoginAbcRequest.full_initialization_id == owner_id
        )
    )


def _require_request(session, tenant_id: int, request_id: int):
    request = session.get(TgPostLoginAbcRequest, request_id)
    if not request or request.tenant_id != tenant_id:
        raise ValueError("post-login ABC request not found")
    return request


def _require_request_ready(session, request) -> None:
    owner = session.get(TgAccountFullInitialization, request.full_initialization_id)
    try:
        if owner:
            _validate_account_fence(session, owner)
    except RuntimeError as exc:
        raise ValueError(str(exc)) from exc
    ready = bool(
        owner
        and owner.two_fa_status == "succeeded"
        and owner.two_fa_evidence_ref
        and owner.profile_status == "succeeded"
        and owner.profile_evidence_ref
        and owner.status == "waiting_abc_approval"
        and request.status == "waiting_approval"
    )
    if not ready:
        raise ValueError("post-login ABC request prerequisites changed")


def _require_approval(
    request,
    *,
    expected_version,
    actor,
    approval_ref,
) -> None:
    if request.request_version != expected_version:
        raise ValueError("post-login ABC request version changed")
    if not actor.strip() or not approval_ref.strip():
        raise ValueError("approver and approval ref are required")
    if actor.strip() == request.requested_by.strip():
        raise ValueError("approver must differ from requester")


def _abc_idempotency_key(request) -> str:
    return f"post-login-full-init:{request.full_initialization_id}:abc"


def _load_claim(session, claim) -> TgAccountFullInitialization:
    owner = session.get(TgAccountFullInitialization, claim.initialization_id)
    if not owner or owner.lease_token != claim.lease_token or owner.stage != claim.stage:
        raise RuntimeError("post-login initialization claim is stale")
    return owner


__all__ = [
    "approve_post_login_abc_request",
    "execute_abc_stage",
    "list_post_login_abc_requests",
    "post_login_abc_request_out",
    "preview_post_login_abc_request",
]

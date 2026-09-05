from __future__ import annotations

from collections import Counter

from sqlalchemy import select

from app.models import AuditLog, TgPostLoginAbcRequest

from .contracts import AuthorizationDrError


RELEASE_BLOCKER = "runtime_image_mismatch"
STOP_ACTION = "停止 post-login exact ABC 自动执行"


def require_post_login_batch(session, context, counts: Counter, *, release_sha: str) -> None:
    post_login_request_snapshot(session, context)
    batch, item = context.batch, context.item
    running = (
        batch.status == "running" and counts == Counter({"running": 1})
        and item.status == item.outcome == "running"
    )
    stopped = counts == Counter({"stopped": 1}) and _release_stopped(session, context, release_sha)
    if batch.target_count != 1 or not (running or stopped):
        raise AuthorizationDrError(
            "online_abc_post_bundle_interrupt_batch_invalid", "Exact post-login boundary changed",
        )


def _release_stopped(session, context, release_sha: str) -> bool:
    batch, item = context.batch, context.item
    return bool(
        batch.status == "stopped" and item.status == "stopped"
        and item.outcome == "runner_blocked" and item.blocker_code == RELEASE_BLOCKER
        and batch.execution_release_sha != release_sha and _release_stop_audit(session, batch.id)
    )


def post_login_request_snapshot(session, context, *, lock: bool = False) -> dict:
    batch = context.batch
    if batch.selection_mode != "post_login_exact":
        return {}
    query = select(TgPostLoginAbcRequest).where(TgPostLoginAbcRequest.abc_batch_id == batch.id)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    requests = list(session.scalars(query))
    request = requests[0] if len(requests) == 1 else None
    fields = ("tenant_id", "requested_by", "approved_by", "approval_ref", "deployed_release_sha")
    statuses = {"running", "manual_required"} if batch.status == "stopped" else {"running"}
    valid = (
        request and request.account_id == context.item.account_id and request.status in statuses
        and all(getattr(request, name) == getattr(batch, name) for name in fields)
    )
    if not valid:
        raise AuthorizationDrError(
            "post_login_post_bundle_request_invalid", "Original post-login approval changed",
        )
    return {"id": request.id, "version": request.request_version, "status": request.status}


def _release_stop_audit(session, batch_id: str) -> bool:
    row = session.scalar(select(AuditLog).where(
        AuditLog.target_type == "tg_authorization_online_abc_batches",
        AuditLog.target_id == batch_id,
        AuditLog.action == STOP_ACTION,
    ).order_by(AuditLog.id.desc()).limit(1))
    return bool(row and f"blocker={RELEASE_BLOCKER};" in row.detail)

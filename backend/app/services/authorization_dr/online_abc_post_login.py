from __future__ import annotations

from sqlalchemy import select

from app.models import (
    TgAuthorizationDrOperation,
    TgAuthorizationOnlineAbcBatch,
    TgAuthorizationOnlineAbcItem,
)
from app.services._common import audit

from .contracts import AuthorizationDrError
from .online_abc import UNKNOWN_OPERATION_STATUSES
from .online_abc_operations import online_abc_item_operations
from .online_abc_runner import run_online_abc_batch


POST_LOGIN_SELECTION_MODE = "post_login_exact"
RUNNABLE_BATCH_STATUSES = {"approved", "running"}


def run_post_login_exact_once(
    session,
    *,
    runtime_release_sha: str,
    poll_seconds: float,
) -> dict:
    batch = _next_batch(session)
    if not batch:
        return {"status": "idle"}
    batch_id = batch.id
    try:
        result = run_online_abc_batch(
            session,
            batch_id,
            requested_by=batch.requested_by,
            approved_by=batch.approved_by,
            approval_ref=batch.approval_ref,
            runtime_release_sha=runtime_release_sha,
            poll_seconds=poll_seconds,
        )
    except AuthorizationDrError as exc:
        return _stop_known_error(session, batch_id, exc.code)
    except Exception as exc:
        blocker = f"post_login_runner_{type(exc).__name__}"
        return _stop_known_error(session, batch_id, blocker)
    return {"status": "processed", "batch_id": batch_id, "result": result}


def _next_batch(session) -> TgAuthorizationOnlineAbcBatch | None:
    return session.scalar(
        select(TgAuthorizationOnlineAbcBatch)
        .where(
            TgAuthorizationOnlineAbcBatch.selection_mode == POST_LOGIN_SELECTION_MODE,
            TgAuthorizationOnlineAbcBatch.status.in_(RUNNABLE_BATCH_STATUSES),
        )
        .order_by(
            TgAuthorizationOnlineAbcBatch.created_at,
            TgAuthorizationOnlineAbcBatch.id,
        )
        .limit(1)
        .with_for_update(skip_locked=True)
    )


def _stop_known_error(session, batch_id: str, blocker: str) -> dict:
    session.rollback()
    batch = session.scalar(
        select(TgAuthorizationOnlineAbcBatch)
        .where(TgAuthorizationOnlineAbcBatch.id == batch_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if not batch:
        return {"status": "blocked", "batch_id": batch_id, "blocker": blocker}
    item = _unfinished_item(session, batch.id)
    if item:
        item.status = "stopped"
        item.outcome = _error_outcome(session, batch, item)
        item.blocker_code = blocker[:100]
        item.version += 1
    batch.status = "stopped"
    batch.version += 1
    audit(
        session,
        tenant_id=batch.tenant_id,
        actor=batch.approved_by,
        action="停止 post-login exact ABC 自动执行",
        target_type="tg_authorization_online_abc_batches",
        target_id=batch.id,
        detail=f"approval_ref={batch.approval_ref}; blocker={blocker[:100]}; no_replay=true",
    )
    session.commit()
    return {"status": "blocked", "batch_id": batch.id, "blocker": blocker}


def _unfinished_item(session, batch_id: str) -> TgAuthorizationOnlineAbcItem | None:
    return session.scalar(
        select(TgAuthorizationOnlineAbcItem)
        .where(
            TgAuthorizationOnlineAbcItem.batch_id == batch_id,
            TgAuthorizationOnlineAbcItem.status.in_(("pending", "running")),
        )
        .order_by(TgAuthorizationOnlineAbcItem.ordinal)
        .limit(1)
        .with_for_update()
    )


def _error_outcome(session, batch, item) -> str:
    operations = online_abc_item_operations(session, batch, item)
    if any(
        operation and operation.status in UNKNOWN_OPERATION_STATUSES
        for operation in operations.values()
    ):
        return "reconcile_unknown"
    unknown = session.scalar(
        select(TgAuthorizationDrOperation.id).where(
            TgAuthorizationDrOperation.account_id == item.account_id,
            TgAuthorizationDrOperation.status.in_(UNKNOWN_OPERATION_STATUSES),
        ).limit(1)
    )
    return "reconcile_unknown" if unknown else "runner_blocked"


__all__ = ["run_post_login_exact_once"]

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    TgAccountFullInitialization,
    TgAccountLoginBatchAttempt,
    TgAccountLoginBatchItem,
    TgAccountLoginPostInitializationBinding,
)
from app.services._common import _now


CORRECTABLE_TERMINAL_OWNER_STATUSES = {
    "succeeded",
    "failed",
    "manual_required",
    "reconcile_unknown",
    "cancelled",
}


def sync_parent_bindings(
    session: Session,
    owner: TgAccountFullInitialization,
) -> None:
    bindings = list(
        session.scalars(
            select(TgAccountLoginPostInitializationBinding).where(
                TgAccountLoginPostInitializationBinding.full_initialization_id == owner.id,
                TgAccountLoginPostInitializationBinding.status == "attached",
            )
        )
    )
    batch_ids: set[int] = set()
    corrections: dict[int, tuple[int, str]] = {}
    for binding in bindings:
        item = session.get(TgAccountLoginBatchItem, binding.login_item_id)
        if not item or item.execution_generation != binding.login_execution_generation:
            continue
        previous_post_init = item.post_initialization_status
        item.post_initialization_status = owner.status
        batch_ids.add(item.batch_id)
        if _should_reopen_item(item, owner):
            _reopen_item(session, item)
        if item.status != "post_initialization_waiting":
            can_correct = bool(
                item.post_initialization_failure_type
                and item.status in {"failed", "unresolved"}
            )
            if can_correct and owner.status in CORRECTABLE_TERMINAL_OWNER_STATUSES:
                _project_item_terminal(session, item, owner)
            if previous_post_init != owner.status:
                corrections[item.batch_id] = (item.id, previous_post_init)
            continue
        _project_item_terminal(session, item, owner)
    _recount_or_finalize_batches(session, batch_ids, corrections)


def _should_reopen_item(item, owner) -> bool:
    post_failure = item.post_initialization_failure_type
    return bool(
        owner.status not in CORRECTABLE_TERMINAL_OWNER_STATUSES
        and item.status in {"failed", "unresolved"}
        and post_failure
        and item.failure_type == post_failure
    )


def _reopen_item(session: Session, item) -> None:
    item.status = "post_initialization_waiting"
    item.phase = "post_initialization_waiting"
    item.failure_type = ""
    item.failure_detail = ""
    item.post_initialization_failure_type = ""
    item.finished_at = None
    item.next_retry_at = None
    item.state_version += 1
    attempt = session.get(TgAccountLoginBatchAttempt, item.current_attempt_id)
    if attempt and attempt.execution_generation == item.execution_generation:
        attempt.phase = "post_initialization_waiting"
        attempt.lease_token = ""
        attempt.lease_expires_at = None
        attempt.state_version += 1


def _project_item_terminal(session, item, owner) -> None:
    if owner.status == "succeeded":
        _finish_item(session, item, status="succeeded", failure_type="", detail="")
        return
    if owner.status in {"failed", "manual_required"}:
        failure = owner.failure_type or f"post_init_{owner.status}"
        _finish_item(
            session,
            item,
            status="failed",
            failure_type=failure,
            detail=owner.failure_detail,
        )
        return
    if owner.status == "reconcile_unknown":
        _finish_item(
            session,
            item,
            status="unresolved",
            failure_type=owner.failure_type or "post_init_remote_unknown",
            detail=owner.failure_detail,
        )
        return
    if owner.status == "cancelled":
        _finish_item(
            session,
            item,
            status="skipped",
            failure_type="manual_interrupted",
            detail=owner.failure_detail,
        )


def _finish_item(session, item, *, status: str, failure_type: str, detail: str) -> None:
    item.status = status
    item.phase = status
    item.failure_type = failure_type
    item.failure_detail = detail
    item.post_initialization_failure_type = failure_type
    item.finished_at = _now()
    item.next_retry_at = None
    item.code_url_ciphertext = None
    item.state_version += 1
    attempt = session.get(TgAccountLoginBatchAttempt, item.current_attempt_id)
    if attempt and attempt.execution_generation == item.execution_generation:
        attempt.phase = status
        attempt.lease_token = ""
        attempt.lease_expires_at = None
        attempt.state_version += 1


def _recount_or_finalize_batches(session, batch_ids, corrections) -> None:
    from app.models import TgAccountLoginBatch
    from app.services.account_login.notifications import (
        finalize_batch_if_terminal,
        record_batch_correction,
    )

    for batch_id in batch_ids:
        batch = session.get(TgAccountLoginBatch, batch_id)
        if batch and batch.status in {
            "completed", "completed_with_manual", "completed_with_failures",
            "completed_with_unresolved", "cancelled",
        } and batch_id in corrections:
            item_id, previous = corrections[batch_id]
            record_batch_correction(
                session,
                batch_id,
                changed_item_id=item_id,
                previous_status=previous,
                status_scope="post_initialization",
            )
            continue
        finalize_batch_if_terminal(session, batch_id)


__all__ = ["sync_parent_bindings"]

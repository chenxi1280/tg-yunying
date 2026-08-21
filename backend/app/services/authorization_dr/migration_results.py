from __future__ import annotations

from sqlalchemy import select

from app.models import (
    AccountStatus,
    TgAccount,
    TgAccountOnlineState,
    TgAuthorizationDrBatch,
    TgAuthorizationDrBatchItem,
    TgAuthorizationDrOperation,
)
from app.services._common import _now

from .contracts import AuthorizationDrError
from .operation_state import mark_item, owned_operation


EXECUTION_TERMINAL_ITEM_STATUSES = ("succeeded", "reconcile_unknown", "manual_required", "failed")
LOGIN_FAILURE_STATUSES = {
    "phone_number_banned": "failed",
    "two_fa_invalid": "manual_required",
}


def mark_login_remote_unknown(session, operation_id: str, *, node_id: str, owner_epoch: int) -> None:
    operation = session.scalar(select(TgAuthorizationDrOperation).where(
        TgAuthorizationDrOperation.id == operation_id,
    ).with_for_update())
    if not operation or operation.owner_node_id != node_id or operation.owner_epoch != owner_epoch:
        raise AuthorizationDrError("execution_node_mismatch", "Operation owner changed")
    if operation.remote_call_state == "confirmed_no_effect" and operation.status in LOGIN_FAILURE_STATUSES.values():
        session.commit()
        return
    if operation.reconcile_status == "applied" and operation.finished_at is not None:
        session.commit()
        return
    operation.remote_call_state = "unknown"
    operation.status = "provision_reconcile_unknown"
    operation.blocker_code = "provision_reconcile_unknown"
    operation.lease_token = ""
    operation.lease_expires_at = None
    operation.operation_version += 1
    mark_item(session, operation, "reconcile_unknown", blocker="provision_reconcile_unknown")
    item = session.get(TgAuthorizationDrBatchItem, operation.batch_item_id)
    if item:
        item.outcome = "provision_reconcile_unknown"
    refresh_migration_batch(session, operation.batch_item_id)
    session.commit()


def mark_login_remote_failed(
    session,
    operation_id: str,
    *,
    node_id: str,
    owner_epoch: int,
    lease_token: str,
    blocker_code: str,
) -> TgAuthorizationDrOperation:
    item_status = LOGIN_FAILURE_STATUSES.get(blocker_code)
    if not item_status:
        raise AuthorizationDrError("login_failure_not_supported", "Login failure is not authoritative")
    operation = owned_operation(
        session,
        operation_id,
        node_id=node_id,
        owner_epoch=owner_epoch,
        lease_token=lease_token,
    )
    if operation.remote_call_state != "started":
        raise AuthorizationDrError("login_failure_state_mismatch", "Remote login has not started")
    operation.remote_call_state = "confirmed_no_effect"
    operation.status = item_status
    operation.blocker_code = blocker_code
    operation.lease_token = ""
    operation.lease_expires_at = None
    operation.finished_at = _now()
    operation.operation_version += 1
    item = session.get(TgAuthorizationDrBatchItem, operation.batch_item_id)
    if not item:
        raise AuthorizationDrError("migration_batch_item_missing", "Migration batch item is unavailable")
    item.status = item_status
    item.outcome = blocker_code
    item.blocker_code = blocker_code
    item.finished_at = _now()
    item.version += 1
    _project_authoritative_login_failure(session, operation.account_id, blocker_code)
    refresh_migration_batch(session, item.id)
    session.commit()
    return operation


def refresh_migration_batch(session, batch_item_id: str) -> None:
    item = session.get(TgAuthorizationDrBatchItem, batch_item_id)
    if not item:
        return
    batch = session.get(TgAuthorizationDrBatch, item.batch_id)
    session.flush()
    statuses = list(session.scalars(select(TgAuthorizationDrBatchItem.status).where(
        TgAuthorizationDrBatchItem.batch_id == item.batch_id,
    )))
    _project_batch_status(batch, statuses)
    batch.version += 1


def project_authoritative_login_failure(session, account_id: int, blocker_code: str) -> None:
    if blocker_code not in LOGIN_FAILURE_STATUSES:
        raise AuthorizationDrError("login_failure_not_supported", "Login failure is not authoritative")
    _project_authoritative_login_failure(session, account_id, blocker_code)


def authoritative_phone_ban_exists(session, account_id: int) -> bool:
    operation_id = session.scalar(select(TgAuthorizationDrOperation.id).where(
        TgAuthorizationDrOperation.account_id == account_id,
        TgAuthorizationDrOperation.blocker_code == "phone_number_banned",
        TgAuthorizationDrOperation.remote_call_state == "confirmed_no_effect",
        TgAuthorizationDrOperation.status == "failed",
    ).limit(1))
    return operation_id is not None


def _project_authoritative_login_failure(session, account_id: int, blocker_code: str) -> None:
    if blocker_code != "phone_number_banned":
        return
    account = session.get(TgAccount, account_id)
    if not account:
        raise AuthorizationDrError("account_not_found", "Migration account does not exist")
    account.status = AccountStatus.BANNED.value
    account.health_score = 0
    state = session.scalar(select(TgAccountOnlineState).where(
        TgAccountOnlineState.tenant_id == account.tenant_id,
        TgAccountOnlineState.account_id == account.id,
    ))
    if not state:
        return
    state.online_status = "login_required"
    state.desired_online = False
    state.desired_sources = []
    state.active_task_count = 0
    state.failure_type = "phone_number_banned"
    state.failure_detail = "Telegram confirmed that this phone number is banned"
    state.last_seen_at = None
    state.stale_after_at = None
    state.next_probe_at = None
    state.updated_at = _now()


def _project_batch_status(batch, statuses: list[str]) -> None:
    now = _now()
    if any(status not in EXECUTION_TERMINAL_ITEM_STATUSES for status in statuses):
        batch.status = "running"
        batch.execution_finished_at = None
        batch.finished_at = None
        return
    batch.execution_finished_at = batch.execution_finished_at or now
    if any(status == "reconcile_unknown" for status in statuses):
        batch.status = "reconcile_required"
        batch.finished_at = None
        return
    batch.finished_at = now
    if all(status == "succeeded" for status in statuses):
        batch.status = "succeeded"
    elif all(status == "manual_required" for status in statuses):
        batch.status = "manual_required"
    elif any(status == "succeeded" for status in statuses):
        batch.status = "partial_success"
    else:
        batch.status = "failed"


__all__ = [
    "LOGIN_FAILURE_STATUSES",
    "authoritative_phone_ban_exists",
    "mark_login_remote_failed",
    "mark_login_remote_unknown",
    "project_authoritative_login_failure",
    "refresh_migration_batch",
]

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, select

from app.models import (
    AuthorizationDrExecutionNode,
    AuthorizationDrRuntimeContract,
    TgAccount,
    TgAccountAuthorization,
    TgAuthorizationDrBatch,
    TgAuthorizationDrBatchItem,
    TgAuthorizationDrOperation,
    TgAuthorizationDrStageFact,
    TgAuthorizationOnlineAbcBatch,
    TgAuthorizationOnlineAbcItem,
    TgAuthorizationOnlineAbcSlotResult,
    TgAuthorizationWakeBundle,
)
from app.services._common import _now

from .contracts import AuthorizationDrError
from .online_abc import UNKNOWN_OPERATION_STATUSES
from .online_abc_manifest import ACTIVE_OPERATION_STATUSES
from .online_abc_manual_outcome import MANUAL_OUTCOME, _batch, _item, _items, _primary_snapshot, _slots
from .online_abc_operations import online_abc_item_operations
from .readiness import MY_NODE_STALE_SECONDS


@dataclass(frozen=True)
class InterruptContext:
    batch: TgAuthorizationOnlineAbcBatch
    item: TgAuthorizationOnlineAbcItem
    slots: dict
    b_operation: TgAuthorizationDrOperation | None
    c_operation: TgAuthorizationDrOperation
    migration_batch: TgAuthorizationDrBatch
    migration_item: TgAuthorizationDrBatchItem
    account: TgAccount
    primary: TgAccountAuthorization
    node: AuthorizationDrExecutionNode


def load_interrupt_context(session, batch_id: str, account_id: int) -> InterruptContext:
    batch = _batch(session, batch_id)
    item = _item(session, batch, account_id)
    slots = _slots(session, item)
    operations = online_abc_item_operations(session, batch, item)
    operation = operations["c"]
    migration_item = session.get(TgAuthorizationDrBatchItem, operation.batch_item_id) if operation else None
    migration_batch = session.get(TgAuthorizationDrBatch, migration_item.batch_id) if migration_item else None
    account = session.get(TgAccount, item.account_id)
    primary = session.get(TgAccountAuthorization, item.primary_authorization_id)
    node = session.get(AuthorizationDrExecutionNode, operation.owner_node_id) if operation else None
    if not all((operation, migration_item, migration_batch, account, primary, node)):
        raise AuthorizationDrError("online_abc_c_precode_interrupt_missing", "Interrupted C facts are incomplete")
    return InterruptContext(
        batch, item, slots, operations["b"], operation,
        migration_batch, migration_item, account, primary, node,
    )


def require_interrupt_boundary(session, context: InterruptContext, release_sha: str) -> Counter:
    counts = Counter(row.status for row in _items(session, context.batch))
    valid = all((
        context.batch.selection_mode == "all_online_accounts",
        context.batch.status == "running",
        context.batch.execution_release_sha != release_sha,
        sum(counts.values()) == context.batch.target_count,
        counts["running"] == 1,
        context.item.status == context.item.outcome == "running",
        bool(counts["pending"]),
        not set(counts) - {"pending", "succeeded", MANUAL_OUTCOME, "running"},
    ))
    if not valid:
        raise AuthorizationDrError("online_abc_c_precode_interrupt_batch_invalid", "Batch boundary changed")
    _require_operation_shape(session, context)
    _require_global_boundary(session, context)
    _primary_snapshot(session, context.item)
    return counts


def lock_interrupt_context(session, batch_id: str, account_id: int) -> None:
    session.expire_all()
    batch = session.scalar(select(TgAuthorizationOnlineAbcBatch).where(
        TgAuthorizationOnlineAbcBatch.id == batch_id,
    ).with_for_update().execution_options(populate_existing=True))
    item = session.scalar(select(TgAuthorizationOnlineAbcItem).where(
        TgAuthorizationOnlineAbcItem.batch_id == batch_id,
        TgAuthorizationOnlineAbcItem.account_id == account_id,
    ).with_for_update().execution_options(populate_existing=True))
    if not batch or not item:
        raise AuthorizationDrError("online_abc_c_precode_interrupt_missing", "Interrupted C item is unavailable")
    context = load_interrupt_context(session, batch_id, account_id)
    for model, row_id in (
        (TgAuthorizationDrOperation, context.c_operation.id),
        (TgAuthorizationDrBatch, context.migration_batch.id),
        (TgAuthorizationDrBatchItem, context.migration_item.id),
        (TgAccount, context.account.id),
        (TgAccountAuthorization, context.primary.id),
        (AuthorizationDrExecutionNode, context.node.id),
        (AuthorizationDrRuntimeContract, 1),
    ):
        session.scalar(select(model).where(model.id == row_id).with_for_update().execution_options(
            populate_existing=True,
        ))
    list(session.scalars(select(TgAuthorizationOnlineAbcSlotResult).where(
        TgAuthorizationOnlineAbcSlotResult.item_id == item.id,
    ).with_for_update().execution_options(populate_existing=True)))


def _require_operation_shape(session, context: InterruptContext) -> None:
    operation = context.c_operation
    bundle_count = session.scalar(select(func.count()).select_from(TgAuthorizationWakeBundle).where(
        TgAuthorizationWakeBundle.operation_id == operation.id,
    ))
    stages = list(session.scalars(select(TgAuthorizationDrStageFact.stage).where(
        TgAuthorizationDrStageFact.operation_id == operation.id,
    ).order_by(TgAuthorizationDrStageFact.created_at)))
    valid = all((
        operation.operation_type in {"migrate_standby_2", "provision_standby_2"},
        operation.logical_slot == "standby_2",
        _operation_matches_frozen_plan(context),
        operation.status == "login_remote_started",
        operation.remote_call_state == "started",
        bool(operation.remote_effect_started_at),
        bool(operation.login_challenge_sent_at),
        not operation.login_code_message_id,
        operation.login_code_received_at is None,
        operation.login_flow_id is None,
        operation.candidate_authorization_id is None,
        operation.reconcile_status == "none",
        operation.reconcile_case_id is None,
        operation.finished_at is None,
        bool(operation.owner_node_id),
        operation.owner_epoch > 0,
        bool(operation.lease_token),
        operation.lease_expires_at is not None and operation.lease_expires_at <= _now(),
        stages == ["remote_login_started"],
        not bundle_count,
        _b_ready(session, context),
        online_abc_item_operations(session, context.batch, context.item)["e4"] is None,
    ))
    if not valid:
        raise AuthorizationDrError("online_abc_c_precode_interrupt_state_invalid", "Interrupted C state changed")


def _operation_matches_frozen_plan(context: InterruptContext) -> bool:
    operation = context.c_operation
    return all((
        operation.tenant_id == context.batch.tenant_id == context.item.tenant_id,
        operation.account_id == context.item.account_id,
        operation.code_source_authorization_id == context.primary.id,
        operation.expected_current_authorization_id == context.primary.id,
        operation.expected_authorization_generation == context.account.authorization_generation,
        operation.expected_authorization_fact_generation == context.account.authorization_fact_generation,
        operation.expected_connection_generation == context.account.connection_generation,
        operation.expected_code_source_fact_version == context.primary.fact_version,
        operation.expected_code_source_user_id_digest == context.primary.telegram_user_id_digest,
        operation.expected_code_source_auth_key_digest == context.primary.auth_key_fingerprint_digest,
    ))


def _b_ready(session, context: InterruptContext) -> bool:
    if context.item.standby_1_plan == "already_qualified":
        return context.b_operation is None
    operation = context.b_operation
    candidate = session.get(TgAccountAuthorization, operation.candidate_authorization_id) if operation else None
    return bool(
        operation and operation.status == "succeeded" and operation.remote_call_state == "succeeded"
        and not operation.blocker_code and candidate and candidate.is_slot_current
        and not candidate.is_current and candidate.provision_region_code == "sv"
        and candidate.status in {"active", "standby"} and candidate.health_status == "healthy"
        and candidate.protected_from_cleanup and candidate.session_ciphertext
        and candidate.telegram_user_id_digest == context.primary.telegram_user_id_digest
        and candidate.auth_key_fingerprint_digest
        and candidate.auth_key_fingerprint_digest != context.primary.auth_key_fingerprint_digest
    )


def _require_global_boundary(session, context: InterruptContext) -> None:
    runtime = session.get(AuthorizationDrRuntimeContract, 1)
    unknown = list(session.scalars(select(TgAuthorizationDrOperation.id).where(
        TgAuthorizationDrOperation.status.in_(UNKNOWN_OPERATION_STATUSES),
    )))
    sensitive = list(session.scalars(select(TgAuthorizationDrOperation.id).where(
        TgAuthorizationDrOperation.status.in_(ACTIVE_OPERATION_STATUSES),
    )))
    valid = all((
        runtime,
        runtime.mode == "migrate",
        runtime.claim_scope_operation_id == context.c_operation.id,
        runtime.required_node_capability_version == context.node.capability_version,
        runtime.required_node_runtime_image_sha == context.node.runtime_image_sha,
        not unknown,
        set(sensitive) == {context.c_operation.id},
        context.node.status == "ready",
        context.node.active_client_count == 0,
        context.node.last_heartbeat_at is not None,
        context.node.last_heartbeat_at > _now() - timedelta(seconds=MY_NODE_STALE_SECONDS),
    ))
    if not valid:
        raise AuthorizationDrError("online_abc_c_precode_interrupt_runtime_active", "Global boundary changed")


__all__ = [
    "InterruptContext",
    "load_interrupt_context",
    "lock_interrupt_context",
    "require_interrupt_boundary",
]

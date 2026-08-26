from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from sqlalchemy import func, select

from app.models import (
    AuthorizationDrExecutionNode,
    AuthorizationDrRuntimeContract,
    TgAccount,
    TgAccountAuthorization,
    TgAuthorizationDrOperation,
    TgAuthorizationDrStageFact,
    TgAuthorizationOnlineAbcBatch,
    TgAuthorizationOnlineAbcItem,
    TgAuthorizationOnlineAbcSlotResult,
    TgAuthorizationWakeBundle,
    TgLoginFlow,
)

from .contracts import AuthorizationDrError
from .online_abc import UNKNOWN_OPERATION_STATUSES
from .online_abc_manifest import ACTIVE_OPERATION_STATUSES
from .online_abc_manual_outcome import MANUAL_OUTCOME, _batch, _item, _items, _slots
from .online_abc_operations import online_abc_item_operations
from .online_abc_release_interrupted_flow import (
    interrupted_login_flows,
    require_empty_interrupted_intent,
)


RELEASE_CHANGED_BOUNDARY = "release_changed_running"
STOPPED_UNKNOWN_BOUNDARY = "stopped_b_prechallenge_unknown"
STOPPED_UNKNOWN_SOURCE_BLOCKER = "TimeoutError"


@dataclass(frozen=True)
class InterruptedContext:
    batch: TgAuthorizationOnlineAbcBatch
    item: TgAuthorizationOnlineAbcItem
    slots: dict
    operation: TgAuthorizationDrOperation
    account: TgAccount
    primary: TgAccountAuthorization
    flows: tuple[TgLoginFlow, ...]


def load_interrupted_context(session, batch_id: str, account_id: int) -> InterruptedContext:
    batch = _batch(session, batch_id)
    item = _item(session, batch, account_id)
    slots = _slots(session, item)
    operation = online_abc_item_operations(session, batch, item)["b"]
    account = session.get(TgAccount, item.account_id)
    primary = session.get(TgAccountAuthorization, item.primary_authorization_id)
    if not operation or not account or not primary:
        raise AuthorizationDrError("online_abc_release_interrupted_missing", "Interrupted B facts are incomplete")
    flows = interrupted_login_flows(session, operation)
    return InterruptedContext(batch, item, slots, operation, account, primary, flows)


def require_interrupted_boundary(
    session, context: InterruptedContext, release_sha: str,
) -> tuple[Counter, str]:
    items = _items(session, context.batch)
    counts = Counter(row.status for row in items)
    boundary = _batch_boundary(context, counts, release_sha)
    _require_operation_shape(session, context, boundary)
    _require_global_boundary(session, context.operation, boundary)
    return counts, boundary


def lock_interrupted_context(session, batch_id: str, account_id: int) -> None:
    session.expire_all()
    batch = session.scalar(select(TgAuthorizationOnlineAbcBatch).where(
        TgAuthorizationOnlineAbcBatch.id == batch_id,
    ).with_for_update().execution_options(populate_existing=True))
    item = session.scalar(select(TgAuthorizationOnlineAbcItem).where(
        TgAuthorizationOnlineAbcItem.batch_id == batch_id,
        TgAuthorizationOnlineAbcItem.account_id == account_id,
    ).with_for_update().execution_options(populate_existing=True))
    if not batch or not item:
        raise AuthorizationDrError("online_abc_release_interrupted_missing", "Interrupted item is unavailable")
    context = load_interrupted_context(session, batch_id, account_id)
    _lock_context_rows(session, context)


def _batch_boundary(context: InterruptedContext, counts: Counter, release_sha: str) -> str:
    common = bool(
        context.batch.selection_mode == "all_online_accounts"
        and sum(counts.values()) == context.batch.target_count
        and counts["pending"]
    )
    if common and _release_changed_batch(context, counts, release_sha):
        return RELEASE_CHANGED_BOUNDARY
    if common and _stopped_unknown_batch(context, counts):
        return STOPPED_UNKNOWN_BOUNDARY
    raise AuthorizationDrError("online_abc_release_interrupted_batch_invalid", "Batch boundary changed")


def _release_changed_batch(context: InterruptedContext, counts: Counter, release_sha: str) -> bool:
    return bool(
        context.batch.status == "running"
        and context.batch.execution_release_sha != release_sha
        and counts["running"] == 1 and not counts["stopped"]
        and context.item.status == context.item.outcome == "running"
        and not set(counts) - {"pending", "succeeded", MANUAL_OUTCOME, "running"}
    )


def _stopped_unknown_batch(context: InterruptedContext, counts: Counter) -> bool:
    return bool(
        context.batch.status == "stopped"
        and counts["stopped"] == 1 and not counts["running"]
        and context.item.status == "stopped"
        and context.item.outcome == context.item.blocker_code == "reconcile_unknown"
        and not set(counts) - {"pending", "succeeded", MANUAL_OUTCOME, "stopped"}
    )


def _require_operation_shape(session, context: InterruptedContext, boundary: str) -> None:
    operation = context.operation
    require_empty_interrupted_intent(operation, context.flows)
    common = all((
        operation.operation_type == "provision_standby_1",
        _operation_matches_frozen_plan(context),
        bool(operation.remote_effect_started_at),
        operation.candidate_authorization_id is None,
        operation.login_flow_id is None,
        operation.login_challenge_sent_at is None,
        not operation.login_code_message_id,
        operation.login_code_received_at is None,
        not operation.owner_node_id,
        operation.owner_epoch == 0,
        not operation.lease_token,
        operation.lease_expires_at is None,
        operation.reconcile_status == "none",
        operation.reconcile_case_id is None,
        operation.finished_at is None,
        context.item.primary_probe_outcome == "pending",
        _no_downstream_operations(session, context),
        not _artifact_or_stage_count(session, operation),
    ))
    valid = common and _boundary_operation_shape(context, boundary)
    if not valid:
        raise AuthorizationDrError("online_abc_release_interrupted_state_invalid", "Interrupted B state changed")


def _boundary_operation_shape(context: InterruptedContext, boundary: str) -> bool:
    operation = context.operation
    b_slot = context.slots["standby_1"]
    c_slot = context.slots["standby_2"]
    if boundary == RELEASE_CHANGED_BOUNDARY:
        return bool(
            operation.status == "login_remote_started"
            and operation.remote_call_state == "started" and not operation.blocker_code
            and b_slot.outcome == c_slot.outcome == "pending"
            and b_slot.operation_id is None and c_slot.operation_id is None
        )
    return bool(
        boundary == STOPPED_UNKNOWN_BOUNDARY
        and operation.status == "reconcile_unknown"
        and operation.remote_call_state == "unknown"
        and operation.blocker_code == STOPPED_UNKNOWN_SOURCE_BLOCKER
        and b_slot.outcome == "reconcile_unknown"
        and b_slot.blocker_code == STOPPED_UNKNOWN_SOURCE_BLOCKER
        and b_slot.operation_id == operation.id
        and c_slot.outcome == "pending" and not c_slot.blocker_code
        and c_slot.operation_id is None
    )


def _operation_matches_frozen_plan(context: InterruptedContext) -> bool:
    operation = context.operation
    item = context.item
    return all((
        operation.tenant_id == context.batch.tenant_id == item.tenant_id,
        operation.account_id == item.account_id,
        operation.logical_slot == "standby_1",
        operation.source_authorization_id == item.primary_authorization_id,
        operation.code_source_authorization_id == item.primary_authorization_id,
        operation.expected_current_authorization_id == item.primary_authorization_id,
        operation.expected_authorization_generation == item.authorization_generation,
        operation.expected_authorization_fact_generation == item.authorization_fact_generation,
        operation.expected_connection_generation == item.connection_generation,
        operation.developer_app_id == item.app_b_id,
        operation.developer_app_credentials_version == item.app_b_credentials_version,
        operation.assignment_version == item.app_b_assignment_version,
    ))


def _no_downstream_operations(session, context: InterruptedContext) -> bool:
    operations = online_abc_item_operations(session, context.batch, context.item)
    return operations == {"b": context.operation, "c": None, "e4": None}


def _artifact_or_stage_count(session, operation) -> int:
    bundles = session.scalar(select(func.count()).select_from(TgAuthorizationWakeBundle).where(
        TgAuthorizationWakeBundle.operation_id == operation.id,
    ))
    stages = session.scalar(select(func.count()).select_from(TgAuthorizationDrStageFact).where(
        TgAuthorizationDrStageFact.operation_id == operation.id,
    ))
    return int(bundles or 0) + int(stages or 0)


def _require_global_boundary(session, operation, boundary: str) -> None:
    runtime = session.get(AuthorizationDrRuntimeContract, 1)
    unknown = set(session.scalars(select(TgAuthorizationDrOperation.id).where(
        TgAuthorizationDrOperation.status.in_(UNKNOWN_OPERATION_STATUSES),
    )))
    sensitive = set(session.scalars(select(TgAuthorizationDrOperation.id).where(
        TgAuthorizationDrOperation.status.in_(ACTIVE_OPERATION_STATUSES),
    )))
    clients = session.scalar(select(func.coalesce(func.sum(
        AuthorizationDrExecutionNode.active_client_count,
    ), 0)).where(AuthorizationDrExecutionNode.region_code == "my"))
    expected_unknown = set() if boundary == RELEASE_CHANGED_BOUNDARY else {operation.id}
    valid = runtime and runtime.mode == "off" and not runtime.claim_scope_operation_id
    if not valid or unknown != expected_unknown or sensitive != {operation.id} or clients:
        raise AuthorizationDrError("online_abc_release_interrupted_runtime_active", "Global boundary changed")


def _lock_context_rows(session, context: InterruptedContext) -> None:
    for model, row_id in (
        (TgAuthorizationDrOperation, context.operation.id),
        (TgAccount, context.account.id),
        (TgAccountAuthorization, context.primary.id),
    ):
        session.scalar(select(model).where(model.id == row_id).with_for_update().execution_options(
            populate_existing=True,
        ))
    list(session.scalars(select(TgAuthorizationOnlineAbcSlotResult).where(
        TgAuthorizationOnlineAbcSlotResult.item_id == context.item.id,
    ).with_for_update().execution_options(populate_existing=True)))
    for flow in context.flows:
        session.scalar(select(TgLoginFlow).where(TgLoginFlow.id == flow.id).with_for_update().execution_options(
            populate_existing=True,
        ))


__all__ = [
    "InterruptedContext",
    "RELEASE_CHANGED_BOUNDARY",
    "STOPPED_UNKNOWN_BOUNDARY",
    "load_interrupted_context",
    "lock_interrupted_context",
    "require_interrupted_boundary",
]

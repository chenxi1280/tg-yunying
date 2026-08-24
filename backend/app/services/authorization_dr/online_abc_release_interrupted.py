from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from sqlalchemy import func, select

from app.models import (
    AuthorizationDrExecutionNode,
    AuthorizationDrRuntimeContract,
    TgAccount,
    TgAccountAuthorization,
    TgAuthorizationDrOperation,
    TgAuthorizationDrReconcileCase,
    TgAuthorizationOnlineAbcBatch,
    TgAuthorizationOnlineAbcItem,
    TgAuthorizationOnlineAbcSlotResult,
    TgAuthorizationWakeBundle,
    TgLoginFlow,
)
from app.services._common import _now, audit

from .contracts import AuthorizationDrError
from .online_abc import UNKNOWN_OPERATION_STATUSES
from .online_abc_manifest import ACTIVE_OPERATION_STATUSES
from .online_abc_manual_outcome import (
    MANUAL_OUTCOME,
    UPSTREAM_MANUAL_BLOCKER,
    _approval,
    _batch,
    _fingerprint,
    _idempotency_key,
    _item,
    _items,
    _mark_slot_manual,
    _primary_snapshot,
    _release_sha,
    _slots,
)
from .online_abc_operations import online_abc_item_operations
from .online_abc_release_interrupted_flow import (
    close_empty_interrupted_intent,
    flow_snapshot,
    interrupted_login_flows,
    require_empty_interrupted_intent,
)


ACTION = "结案发布中断的 ABC B pre-flow"
BLOCKER = "release_interrupted_pre_flow_unproven"
CLASSIFICATION = "release_interrupted_remote_unproven"
REF_PATTERN = re.compile(r"[A-Za-z0-9:._/-]{1,160}")


@dataclass(frozen=True)
class InterruptedContext:
    batch: TgAuthorizationOnlineAbcBatch
    item: TgAuthorizationOnlineAbcItem
    slots: dict
    operation: TgAuthorizationDrOperation
    account: TgAccount
    primary: TgAccountAuthorization
    flows: tuple[TgLoginFlow, ...]


def preview_release_interrupted_b(
    session,
    batch_id: str,
    account_id: int,
    *,
    runtime_release_sha: str,
    idempotency_key: str,
    requested_by: str,
    approved_by: str,
    approval_ref: str,
    interruption_ref: str,
) -> dict:
    context = _context(session, batch_id, account_id)
    approval = _approval(
        context.batch,
        requested_by=requested_by,
        approved_by=approved_by,
        approval_ref=approval_ref,
    )
    release_sha = _release_sha(runtime_release_sha)
    key = _idempotency_key(idempotency_key)
    interruption = _interruption_ref(interruption_ref)
    counts = _require_boundary(session, context, release_sha)
    payload = _payload(
        session,
        context,
        counts=counts,
        release_sha=release_sha,
        key=key,
        approval=approval,
        interruption_ref=interruption,
    )
    return {**payload, "fingerprint": _fingerprint(payload)}


def apply_release_interrupted_b(
    session,
    batch_id: str,
    account_id: int,
    *,
    runtime_release_sha: str,
    idempotency_key: str,
    expected_fingerprint: str,
    requested_by: str,
    approved_by: str,
    approval_ref: str,
    interruption_ref: str,
) -> dict:
    existing = _existing_result(
        session, batch_id, account_id=account_id, key=idempotency_key,
    )
    if existing:
        return _idempotent(existing, expected_fingerprint)
    _lock_context(session, batch_id, account_id)
    existing = _existing_result(
        session, batch_id, account_id=account_id, key=idempotency_key,
    )
    if existing:
        return _idempotent(existing, expected_fingerprint)
    preview = preview_release_interrupted_b(
        session,
        batch_id,
        account_id,
        runtime_release_sha=runtime_release_sha,
        idempotency_key=idempotency_key,
        requested_by=requested_by,
        approved_by=approved_by,
        approval_ref=approval_ref,
        interruption_ref=interruption_ref,
    )
    if preview["fingerprint"] != expected_fingerprint:
        raise AuthorizationDrError("migration_fingerprint_conflict", "Interrupted B preview changed")
    _apply_transition(session, _context(session, batch_id, account_id), preview)
    session.commit()
    return {**_result(session, batch_id, account_id), "already_applied": False}


def readback_release_interrupted_b(
    session, batch_id: str, account_id: int, *, idempotency_key: str,
) -> dict:
    existing = _existing_result(
        session, batch_id, account_id=account_id, key=idempotency_key,
    )
    if not existing:
        raise AuthorizationDrError(
            "online_abc_release_interrupted_not_found", "Interrupted B audit is unavailable",
        )
    return {**existing, "already_applied": True}


def _context(session, batch_id: str, account_id: int) -> InterruptedContext:
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


def _require_boundary(session, context: InterruptedContext, release_sha: str) -> Counter:
    items = _items(session, context.batch)
    counts = Counter(row.status for row in items)
    valid_statuses = {"pending", "succeeded", MANUAL_OUTCOME, "running"}
    valid = (
        context.batch.selection_mode == "all_online_accounts"
        and context.batch.status == "running"
        and context.batch.execution_release_sha != release_sha
        and len(items) == context.batch.target_count
        and counts["running"] == 1
        and context.item.status == context.item.outcome == "running"
        and bool(counts["pending"])
        and not set(counts) - valid_statuses
    )
    if not valid:
        raise AuthorizationDrError("online_abc_release_interrupted_batch_invalid", "Batch boundary changed")
    _require_operation_shape(session, context)
    _require_global_boundary(session, context.operation)
    _primary_snapshot(session, context.item)
    return counts


def _require_operation_shape(session, context: InterruptedContext) -> None:
    operation = context.operation
    b_slot = context.slots["standby_1"]
    c_slot = context.slots["standby_2"]
    operations = online_abc_item_operations(session, context.batch, context.item)
    bundle_count = session.scalar(select(func.count()).select_from(TgAuthorizationWakeBundle).where(
        TgAuthorizationWakeBundle.operation_id == operation.id,
    ))
    require_empty_interrupted_intent(operation, context.flows)
    valid = all((
        operation.operation_type == "provision_standby_1",
        _operation_matches_frozen_plan(context),
        operation.status == "login_remote_started",
        operation.remote_call_state == "started",
        bool(operation.remote_effect_started_at),
        operation.candidate_authorization_id is None,
        operation.login_flow_id is None,
        operation.login_challenge_sent_at is None,
        not operation.login_code_message_id,
        operation.login_code_received_at is None,
        not operation.blocker_code,
        not operation.owner_node_id,
        not operation.lease_token,
        operation.lease_expires_at is None,
        operation.reconcile_status == "none",
        operation.reconcile_case_id is None,
        operation.finished_at is None,
        context.item.primary_probe_outcome == "pending",
        b_slot.outcome == c_slot.outcome == "pending",
        b_slot.operation_id is None,
        c_slot.operation_id is None,
        operations == {"b": operation, "c": None, "e4": None},
    ))
    if not valid or bundle_count:
        raise AuthorizationDrError("online_abc_release_interrupted_state_invalid", "Interrupted B state changed")


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


def _require_global_boundary(session, operation) -> None:
    runtime = session.get(AuthorizationDrRuntimeContract, 1)
    unknown = list(session.scalars(select(TgAuthorizationDrOperation.id).where(
        TgAuthorizationDrOperation.status.in_(UNKNOWN_OPERATION_STATUSES),
    )))
    sensitive = list(session.scalars(select(TgAuthorizationDrOperation.id).where(
        TgAuthorizationDrOperation.status.in_(ACTIVE_OPERATION_STATUSES),
    )))
    clients = session.scalar(select(func.coalesce(func.sum(
        AuthorizationDrExecutionNode.active_client_count,
    ), 0)).where(AuthorizationDrExecutionNode.region_code == "my"))
    valid = runtime and runtime.mode == "off" and not runtime.claim_scope_operation_id
    if not valid or unknown or set(sensitive) != {operation.id} or clients:
        raise AuthorizationDrError("online_abc_release_interrupted_runtime_active", "Global boundary changed")


def _payload(
    session,
    context: InterruptedContext,
    *,
    counts: Counter,
    release_sha: str,
    key: str,
    approval: tuple[str, str, str],
    interruption_ref: str,
) -> dict:
    operation = context.operation
    return {
        "batch_id": context.batch.id,
        "batch_version": context.batch.version,
        "previous_execution_release_sha": context.batch.execution_release_sha,
        "runtime_release_sha": release_sha,
        "account_id": context.item.account_id,
        "item_id": context.item.id,
        "item_version": context.item.version,
        "b_slot_id": context.slots["standby_1"].id,
        "b_slot_version": context.slots["standby_1"].version,
        "c_slot_id": context.slots["standby_2"].id,
        "c_slot_version": context.slots["standby_2"].version,
        "operation_id": operation.id,
        "operation_version": operation.operation_version,
        "operation_status": operation.status,
        "remote_call_state": operation.remote_call_state,
        "remote_effect_started_at": str(operation.remote_effect_started_at),
        "interrupted_flow": flow_snapshot(context.flows[0] if context.flows else None),
        "primary": _primary_snapshot(session, context.item),
        "pending_count": counts["pending"],
        "manual_count": counts[MANUAL_OUTCOME],
        "classification": CLASSIFICATION,
        "blocker_code": BLOCKER,
        "interruption_ref": interruption_ref,
        "idempotency_key": key,
        "requested_by": approval[0],
        "approved_by": approval[1],
        "approval_ref": approval[2],
    }


def _lock_context(session, batch_id: str, account_id: int) -> None:
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
    context = _context(session, batch_id, account_id)
    for model, row_id in (
        (TgAuthorizationDrOperation, context.operation.id),
        (TgAccount, context.account.id),
        (TgAccountAuthorization, context.primary.id),
    ):
        session.scalar(select(model).where(model.id == row_id).with_for_update().execution_options(
            populate_existing=True,
        ))
    list(session.scalars(select(TgAuthorizationOnlineAbcSlotResult).where(
        TgAuthorizationOnlineAbcSlotResult.item_id == item.id,
    ).with_for_update().execution_options(populate_existing=True)))
    for flow in context.flows:
        session.scalar(select(TgLoginFlow).where(TgLoginFlow.id == flow.id).with_for_update().execution_options(
            populate_existing=True,
        ))


def _apply_transition(session, context: InterruptedContext, preview: dict) -> None:
    operation = context.operation
    case = TgAuthorizationDrReconcileCase(
        tenant_id=operation.tenant_id,
        account_id=operation.account_id,
        operation_id=operation.id,
        status="applied",
        classification=CLASSIFICATION,
        recommended_transition=MANUAL_OUTCOME,
        blocker_code=BLOCKER,
        expected_operation_version=operation.operation_version,
        expected_item_version=context.item.version,
        expected_source_fact_version=context.primary.fact_version,
        expected_owner_epoch=operation.owner_epoch,
        expected_node_id=operation.owner_node_id,
        expected_runtime_image_sha=preview["runtime_release_sha"],
        evidence_fingerprint=preview["fingerprint"],
        evidence_manifest=_evidence_manifest(preview),
        persisted_artifact_state="none",
        requested_by=preview["requested_by"],
        applied_by=preview["approved_by"],
        approval_ref=preview["approval_ref"],
        apply_idempotency_key=preview["idempotency_key"],
        applied_at=_now(),
    )
    session.add(case)
    session.flush()
    close_empty_interrupted_intent(
        operation,
        context.flows[0] if context.flows else None,
        blocker_code=BLOCKER,
        interruption_ref=preview["interruption_ref"],
    )
    _close_operation(operation, case)
    _mark_slot_manual(context.slots["standby_1"], BLOCKER)
    context.slots["standby_1"].operation_id = operation.id
    _mark_slot_manual(context.slots["standby_2"], UPSTREAM_MANUAL_BLOCKER)
    _close_item_and_batch(context, preview)
    _audit_transition(session, context, preview)


def _close_operation(operation, case) -> None:
    operation.status = MANUAL_OUTCOME
    operation.remote_call_state = "reconciled_hold"
    operation.blocker_code = BLOCKER
    operation.reconcile_case_id = case.id
    operation.reconcile_status = "applied"
    operation.reconciled_at = _now()
    operation.finished_at = _now()
    operation.operation_version += 1


def _close_item_and_batch(context: InterruptedContext, preview: dict) -> None:
    context.item.status = MANUAL_OUTCOME
    context.item.outcome = MANUAL_OUTCOME
    context.item.blocker_code = BLOCKER
    context.item.finished_at = _now()
    context.item.version += 1
    context.batch.execution_release_sha = preview["runtime_release_sha"]
    context.batch.status = "running" if preview["pending_count"] else "completed_with_manual"
    context.batch.version += 1


def _evidence_manifest(preview: dict) -> dict:
    return {key: preview[key] for key in (
        "classification", "blocker_code", "interruption_ref", "previous_execution_release_sha",
        "runtime_release_sha", "operation_status", "remote_call_state", "remote_effect_started_at",
        "interrupted_flow",
    )}


def _audit_transition(session, context: InterruptedContext, preview: dict) -> None:
    audit(
        session,
        tenant_id=context.batch.tenant_id,
        actor=preview["approved_by"],
        action=ACTION,
        target_type="tg_authorization_dr_operation",
        target_id=context.operation.id,
        detail=(
            f"account_id={context.item.account_id}; approval_ref={preview['approval_ref']}; "
            f"idempotency_key={preview['idempotency_key']}; fingerprint={preview['fingerprint']}; "
            f"interruption_ref={preview['interruption_ref']}; blocker={BLOCKER}; "
            f"execution_release={preview['previous_execution_release_sha']}->{preview['runtime_release_sha']}"
        ),
    )


def _existing_result(
    session, batch_id: str, *, account_id: int, key: str,
) -> dict | None:
    normalized = _idempotency_key(key)
    context = _context(session, batch_id, account_id)
    case = session.scalar(select(TgAuthorizationDrReconcileCase).where(
        TgAuthorizationDrReconcileCase.operation_id == context.operation.id,
    ))
    if not case or case.classification != CLASSIFICATION:
        return None
    if case.apply_idempotency_key != normalized:
        raise AuthorizationDrError("idempotency_key_conflict", "Interrupted B key was already used")
    return _result(session, batch_id, account_id)


def _idempotent(existing: dict, fingerprint: str) -> dict:
    if existing["fingerprint"] != fingerprint:
        raise AuthorizationDrError("idempotency_key_conflict", "Interrupted B fingerprint changed")
    return {**existing, "already_applied": True}


def _result(session, batch_id: str, account_id: int) -> dict:
    context = _context(session, batch_id, account_id)
    case = session.get(TgAuthorizationDrReconcileCase, context.operation.reconcile_case_id)
    manifest = case.evidence_manifest if case else {}
    return {
        "batch_id": context.batch.id,
        "batch_status": context.batch.status,
        "batch_version": context.batch.version,
        "execution_release_sha": context.batch.execution_release_sha,
        "account_id": context.item.account_id,
        "item_status": context.item.status,
        "item_outcome": context.item.outcome,
        "item_version": context.item.version,
        "b_outcome": context.slots["standby_1"].outcome,
        "c_outcome": context.slots["standby_2"].outcome,
        "operation_id": context.operation.id,
        "operation_status": context.operation.status,
        "operation_version": context.operation.operation_version,
        "remote_call_state": context.operation.remote_call_state,
        "remote_effect_started_at": str(context.operation.remote_effect_started_at),
        "blocker_code": context.operation.blocker_code,
        "reconcile_status": context.operation.reconcile_status,
        "reconcile_case_id": context.operation.reconcile_case_id,
        "classification": case.classification if case else "",
        "interruption_ref": manifest.get("interruption_ref", ""),
        "interrupted_flow": flow_snapshot(context.flows[0] if context.flows else None),
        "primary": _primary_snapshot(session, context.item),
        "fingerprint": case.evidence_fingerprint if case else "",
    }


def _interruption_ref(value: str) -> str:
    normalized = value.strip()
    if not REF_PATTERN.fullmatch(normalized):
        raise AuthorizationDrError("interruption_ref_invalid", "Interruption reference is invalid")
    return normalized


__all__ = [
    "apply_release_interrupted_b",
    "preview_release_interrupted_b",
    "readback_release_interrupted_b",
]

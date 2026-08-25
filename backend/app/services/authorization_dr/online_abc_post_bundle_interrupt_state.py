from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, select

from app.models import (
    AuthorizationDrExecutionNode,
    AuthorizationDrRuntimeContract,
    TgAccountAuthorization,
    TgAuthorizationDrOperation,
    TgAuthorizationDrStageFact,
    TgAuthorizationRestoreProbeFact,
    TgAuthorizationSlotDecision,
    TgAuthorizationWakeBundle,
    TgAuthorizationWakeBundleCopy,
    TgAuthorizationWakeInventoryEntry,
)
from app.services._common import _now

from .contracts import AuthorizationDrError
from .online_abc import UNKNOWN_OPERATION_STATUSES
from .online_abc_c_precode_interrupt_state import (
    InterruptContext,
    _b_ready,
    _operation_matches_frozen_plan,
    load_interrupt_context,
    lock_interrupt_context,
)
from .online_abc_manifest import ACTIVE_OPERATION_STATUSES
from .online_abc_manual_outcome import MANUAL_OUTCOME, _items, _primary_snapshot
from .online_abc_operations import online_abc_item_operations
from .readiness import MY_NODE_STALE_SECONDS
from .wake_bundle import valid_copy_kinds


BOUNDARY = "post_bundle_pre_restore_probe"
STAGES = (
    "remote_login_started",
    "remote_login_confirmed",
    "local_copy_verified",
    "snapshot_copy_verified",
    "inventory_persisted",
    "central_receipt_committed",
)


@dataclass(frozen=True)
class PostBundleContext:
    interrupt: InterruptContext
    candidate: TgAccountAuthorization
    bundle: TgAuthorizationWakeBundle
    copies: tuple[TgAuthorizationWakeBundleCopy, ...]
    inventory: TgAuthorizationWakeInventoryEntry


def load_post_bundle_context(session, batch_id: str, account_id: int) -> PostBundleContext:
    interrupt = load_interrupt_context(session, batch_id, account_id)
    operation = interrupt.c_operation
    candidate = session.get(TgAccountAuthorization, operation.candidate_authorization_id)
    bundle = session.scalar(select(TgAuthorizationWakeBundle).where(
        TgAuthorizationWakeBundle.operation_id == operation.id,
    ))
    copies = tuple(session.scalars(select(TgAuthorizationWakeBundleCopy).where(
        TgAuthorizationWakeBundleCopy.bundle_id == bundle.id if bundle else False,
    ).order_by(TgAuthorizationWakeBundleCopy.copy_kind)))
    inventory = session.scalar(select(TgAuthorizationWakeInventoryEntry).where(
        TgAuthorizationWakeInventoryEntry.operation_id == operation.id,
    ))
    if not candidate or not bundle or not inventory:
        raise AuthorizationDrError(
            "online_abc_post_bundle_interrupt_missing",
            "Interrupted post-bundle C facts are incomplete",
        )
    return PostBundleContext(interrupt, candidate, bundle, copies, inventory)


def require_post_bundle_boundary(
    session, context: PostBundleContext, release_sha: str,
) -> Counter:
    counts = Counter(row.status for row in _items(session, context.interrupt.batch))
    _require_batch(context.interrupt, counts, release_sha)
    _require_operation(session, context)
    _require_artifacts(session, context)
    _require_global(session, context)
    _primary_snapshot(session, context.interrupt.item)
    return counts


def lock_post_bundle_context(session, batch_id: str, account_id: int) -> None:
    lock_interrupt_context(session, batch_id, account_id)
    context = load_post_bundle_context(session, batch_id, account_id)
    rows = (
        (TgAccountAuthorization, context.candidate.id),
        (TgAuthorizationWakeBundle, context.bundle.id),
        (TgAuthorizationWakeInventoryEntry, context.inventory.id),
    )
    for model, row_id in rows:
        session.scalar(select(model).where(model.id == row_id).with_for_update().execution_options(
            populate_existing=True,
        ))
    list(session.scalars(select(TgAuthorizationWakeBundleCopy).where(
        TgAuthorizationWakeBundleCopy.bundle_id == context.bundle.id,
    ).with_for_update().execution_options(populate_existing=True)))


def _require_batch(context: InterruptContext, counts: Counter, release_sha: str) -> None:
    valid = all((
        context.batch.selection_mode == "all_online_accounts",
        context.batch.status == "running",
        context.batch.execution_release_sha != release_sha,
        sum(counts.values()) == context.batch.target_count,
        counts["running"] == 1,
        bool(counts["pending"]),
        not set(counts) - {"pending", "succeeded", MANUAL_OUTCOME, "running"},
        context.item.status == context.item.outcome == "running",
    ))
    if not valid:
        raise AuthorizationDrError(
            "online_abc_post_bundle_interrupt_batch_invalid", "Post-bundle batch boundary changed",
        )


def _require_operation(session, context: PostBundleContext) -> None:
    base = context.interrupt
    operation = base.c_operation
    stages = tuple(session.scalars(select(TgAuthorizationDrStageFact.stage).where(
        TgAuthorizationDrStageFact.operation_id == operation.id,
    ).order_by(TgAuthorizationDrStageFact.created_at, TgAuthorizationDrStageFact.id)))
    valid = all((
        operation.operation_type == "provision_standby_2",
        operation.logical_slot == "standby_2",
        operation.source_authorization_id is None,
        base.item.standby_2_plan == "provision",
        base.item.source_c_authorization_id is None,
        base.migration_item.expected_source_authorization_id is None,
        base.migration_item.expected_source_fact_version == 0,
        base.migration_item.expected_source_generation == 0,
        base.migration_item.status == "running",
        base.migration_item.outcome == "pending",
        base.migration_batch.status == "approved",
        _operation_matches_frozen_plan(base),
        operation.status == "bundle_copies_verified",
        operation.remote_call_state == "confirmed",
        bool(operation.remote_effect_started_at),
        operation.candidate_authorization_id == context.candidate.id,
        bool(operation.owner_node_id),
        operation.owner_epoch > 0,
        bool(operation.lease_token),
        operation.lease_expires_at is not None,
        operation.lease_expires_at <= _now(),
        not operation.blocker_code,
        operation.reconcile_status == "none",
        operation.reconcile_case_id is None,
        operation.finished_at is None,
        stages == STAGES,
        _b_ready(session, base),
        online_abc_item_operations(session, base.batch, base.item)["e4"] is None,
    ))
    if not valid:
        raise AuthorizationDrError(
            "online_abc_post_bundle_interrupt_state_invalid", "Post-bundle operation changed",
        )


def _require_artifacts(session, context: PostBundleContext) -> None:
    operation = context.interrupt.c_operation
    candidate = context.candidate
    bundle = context.bundle
    copy_kinds = {row.copy_kind for row in context.copies}
    valid = all((
        candidate.account_id == operation.account_id,
        candidate.logical_slot == "standby_2",
        candidate.slot_generation == operation.target_generation,
        candidate.developer_app_id == operation.developer_app_id,
        not candidate.is_current,
        not candidate.is_slot_current,
        candidate.provision_region_code == "my",
        candidate.status == "candidate",
        candidate.health_status == "unknown",
        candidate.dr_state == "bundle_copies_verified",
        candidate.protected_from_cleanup,
        candidate.wake_bundle_id == bundle.id,
        candidate.telegram_user_id_digest == context.interrupt.primary.telegram_user_id_digest,
        bool(candidate.auth_key_fingerprint_digest),
        candidate.auth_key_fingerprint_digest != context.interrupt.primary.auth_key_fingerprint_digest,
        bundle.operation_id == operation.id,
        bundle.authorization_id == candidate.id,
        bundle.bundle_generation == operation.target_generation,
        bundle.receipt_status == "copies_verified",
        bundle.kms_decrypt_status == "verified",
        bool(bundle.wrapped_dek_ciphertext),
        bool(bundle.kms_key_ref_digest),
        bool(bundle.kms_key_version),
        bundle.telegram_user_id_digest == candidate.telegram_user_id_digest,
        bundle.auth_key_fingerprint_digest == candidate.auth_key_fingerprint_digest,
        bundle.recoverable_copy_count == 2,
        not bundle.is_active,
        len(context.copies) == 2,
        valid_copy_kinds(copy_kinds),
        all(_copy_matches(row, bundle) for row in context.copies),
        _inventory_matches(context),
        not _downstream_artifact(session, context),
    ))
    if not valid:
        raise AuthorizationDrError(
            "online_abc_post_bundle_interrupt_artifact_invalid", "Post-bundle artifacts changed",
        )


def _copy_matches(row: TgAuthorizationWakeBundleCopy, bundle: TgAuthorizationWakeBundle) -> bool:
    return bool(
        row.ciphertext_digest == bundle.ciphertext_digest
        and row.object_ref_digest and row.immutable_version
        and row.write_receipt_digest and row.readback_receipt_digest
        and row.write_verified_at and row.readback_verified_at and row.decrypt_verified_at
    )


def _inventory_matches(context: PostBundleContext) -> bool:
    row = context.inventory
    return bool(
        row.operation_id == context.interrupt.c_operation.id
        and row.account_id == context.interrupt.item.account_id
        and row.authorization_id == context.candidate.id
        and row.bundle_id == context.bundle.id
        and row.event_type == "bundle_receipt_committed"
        and row.inventory_sequence > 0
        and row.manifest_digest
    )


def _downstream_artifact(session, context: PostBundleContext) -> bool:
    probe = session.scalar(select(TgAuthorizationRestoreProbeFact.id).where(
        TgAuthorizationRestoreProbeFact.bundle_id == context.bundle.id,
    ).limit(1))
    decision = session.scalar(select(TgAuthorizationSlotDecision.id).where(
        TgAuthorizationSlotDecision.new_authorization_id == context.candidate.id,
    ).limit(1))
    return bool(probe or decision)


def _require_global(session, context: PostBundleContext) -> None:
    base = context.interrupt
    operation = base.c_operation
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
    valid = all((
        runtime,
        runtime.mode == "migrate",
        runtime.claim_scope_operation_id == operation.id,
        runtime.required_node_capability_version == base.node.capability_version,
        runtime.required_node_runtime_image_sha == base.node.runtime_image_sha,
        not unknown,
        set(sensitive) == {operation.id},
        clients == 0,
        base.node.status == "ready",
        base.node.active_client_count == 0,
        base.node.last_heartbeat_at is not None,
        base.node.last_heartbeat_at > _now() - timedelta(seconds=MY_NODE_STALE_SECONDS),
    ))
    if not valid:
        raise AuthorizationDrError(
            "online_abc_post_bundle_interrupt_runtime_active", "Post-bundle runtime changed",
        )


__all__ = [
    "BOUNDARY",
    "PostBundleContext",
    "load_post_bundle_context",
    "lock_post_bundle_context",
    "require_post_bundle_boundary",
]

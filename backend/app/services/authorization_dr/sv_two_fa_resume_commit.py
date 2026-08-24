from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from app.models import AccountStatus, TgAccountAuthorization, TgAuthorizationDrOperation, TgAuthorizationDrReconcileCase
from app.security import encrypt_secret, encrypt_session
from app.services._common import _now, audit
from app.services.account_two_fa import record_managed_two_fa_password
from app.timezone import as_beijing_aware

from .contracts import AuthorizationDrError
from .sv_login_recovery import _next_slot_generation, _standby_target_slot

if TYPE_CHECKING:
    from .sv_two_fa_resume import ResumeContext


RECOVERY_CLASSIFICATION = "sv_two_fa_session_recovered"
INVALID_CLASSIFICATION = "sv_two_fa_invalid"


def persist_candidate(session, context: ResumeContext, remote: dict, *, actor: str):
    _retain_target_rows(session, context)
    identity = remote["identity"]
    target_slot = _standby_target_slot(context.primary)
    asset = TgAccountAuthorization(
        tenant_id=context.operation.tenant_id, account_id=context.account.id, role="standby_1",
        logical_slot=target_slot,
        slot_generation=_next_slot_generation(session, context.account.id, target_slot),
        is_slot_current=True, provision_region_code="sv", developer_app_id=context.app.id,
        developer_app_api_id_snapshot=context.app.api_id,
        proxy_id=context.proxy.id if context.proxy else None,
        session_ciphertext=encrypt_session(remote["raw_session"]), status="standby",
        health_status="healthy", derived_status="healthy", is_current=False,
        remote_authorization_state="active", protected_from_cleanup=True, dr_state="dormant_ready",
        telegram_authorization_hash_ciphertext=encrypt_secret(identity.authorization_hash),
        auth_key_fingerprint_digest=identity.auth_key_fingerprint_digest,
        telegram_user_id_digest=identity.telegram_user_id_digest,
        telegram_login_at=as_beijing_aware(context.flow.challenge_sent_at),
        last_health_check_at=_now(), last_success_at=_now(), fact_version=2, created_by=actor,
    )
    session.add(asset)
    session.flush()
    return asset


def close_success(
    session, context: ResumeContext, asset, *, payload, remote, fingerprint, actor, approval_ref, key,
) -> None:
    manifest = {**payload, "remote_set_digest": remote["remote_set_digest"],
                "remote_device_count": remote["remote_device_count"],
                "candidate_hash_digest": remote["candidate_hash_digest"],
                "candidate_fingerprint_digest": remote["candidate_fingerprint_digest"],
                "authorization_hash_source": remote["hash_source"]}
    case = _new_case(
        context, payload=payload, manifest=manifest, fingerprint=fingerprint, actor=actor,
        approval_ref=approval_ref, key=key, classification=RECOVERY_CLASSIFICATION,
        transition="succeeded", artifact_state="central_candidate_committed",
    )
    session.add(case)
    session.flush()
    _finish_operation(context, case, asset)
    record_managed_two_fa_password(session, context.account, remote["fixed_password"])
    _clear_flow(context.flow, asset.id, AccountStatus.ACTIVE.value)
    _audit_close(
        session, context, actor=actor, approval_ref=approval_ref, key=key,
        classification=RECOVERY_CLASSIFICATION,
    )


def close_invalid(
    session, context: ResumeContext, *, payload, fingerprint, actor, approval_ref, key,
) -> None:
    case = _new_case(
        context, payload=payload, manifest=payload, fingerprint=fingerprint, actor=actor,
        approval_ref=approval_ref, key=key, classification=INVALID_CLASSIFICATION,
        transition="manual_required", artifact_state="confirmed_no_effect",
    )
    session.add(case)
    session.flush()
    operation = context.operation
    operation.status = "manual_required"
    operation.blocker_code = "two_fa_invalid"
    operation.remote_call_state = "confirmed_no_effect"
    _apply_reconcile_case(operation, case)
    _clear_flow(context.flow, None, AccountStatus.ERROR.value)
    _audit_close(
        session, context, actor=actor, approval_ref=approval_ref, key=key,
        classification=INVALID_CLASSIFICATION,
    )


def idempotent_result(
    session, operation_id, *, tenant_id, requested_by, actor, key, fingerprint,
):
    operation = session.get(TgAuthorizationDrOperation, operation_id)
    case = session.scalar(select(TgAuthorizationDrReconcileCase).where(
        TgAuthorizationDrReconcileCase.operation_id == operation_id,
    ))
    if not operation or operation.tenant_id != tenant_id or not case:
        return None
    if case.classification not in {RECOVERY_CLASSIFICATION, INVALID_CLASSIFICATION}:
        return None
    same_request = case.requested_by == requested_by.strip() and case.applied_by == actor.strip()
    if case.apply_idempotency_key != key or case.evidence_fingerprint != fingerprint or not same_request:
        raise AuthorizationDrError("reconcile_evidence_conflict", "SV 2FA resume idempotency changed")
    return result(operation, fingerprint, case.classification)


def result(operation, fingerprint, classification) -> dict:
    return {
        "operation_id": operation.id,
        "account_id": operation.account_id,
        "operation_status": operation.status,
        "operation_version": operation.operation_version,
        "candidate_authorization_id": operation.candidate_authorization_id,
        "remote_call_state": operation.remote_call_state,
        "reconcile_status": operation.reconcile_status,
        "classification": classification,
        "evidence_fingerprint": fingerprint,
    }


def _retain_target_rows(session, context: ResumeContext) -> None:
    rows = list(session.scalars(select(TgAccountAuthorization).where(
        TgAccountAuthorization.account_id == context.account.id,
        TgAccountAuthorization.id != context.primary.id,
        TgAccountAuthorization.logical_slot == _standby_target_slot(context.primary),
        TgAccountAuthorization.disabled_at.is_(None),
    ).with_for_update()))
    for row in rows:
        row.role = "standby_repair"
        row.logical_slot = "standby_repair"
        row.is_slot_current = False
        row.status = "needs_repair"
        row.health_status = "unknown"
        row.derived_status = "needs_repair"
        row.protected_from_cleanup = True
        row.failure_reason = "Retained after SV fixed-2FA recovery"
        row.fact_version += 1


def _finish_operation(context: ResumeContext, case, asset) -> None:
    operation = context.operation
    operation.candidate_authorization_id = asset.id
    operation.logical_slot = asset.logical_slot
    operation.status = "succeeded"
    operation.blocker_code = ""
    operation.remote_call_state = "succeeded"
    _apply_reconcile_case(operation, case)


def _apply_reconcile_case(operation, case) -> None:
    operation.reconcile_case_id = case.id
    operation.reconcile_status = "applied"
    operation.reconciled_at = operation.finished_at = _now()
    operation.operation_version += 1


def _new_case(
    context: ResumeContext, *, payload, manifest, fingerprint, actor, approval_ref, key,
    classification, transition, artifact_state,
):
    return TgAuthorizationDrReconcileCase(
        tenant_id=context.operation.tenant_id, account_id=context.account.id,
        operation_id=context.operation.id, status="applied", classification=classification,
        recommended_transition=transition, blocker_code="PasswordHashInvalidError",
        expected_operation_version=payload["operation_version"],
        expected_item_version=payload["item_version"],
        expected_source_fact_version=payload["primary_fact_version"],
        expected_owner_epoch=context.operation.owner_epoch, expected_node_id="sv",
        expected_runtime_image_sha=payload["runtime_image_sha"], evidence_fingerprint=fingerprint,
        evidence_manifest=manifest, persisted_artifact_state=artifact_state,
        requested_by=payload["requested_by"], applied_by=actor, approval_ref=approval_ref,
        apply_idempotency_key=key, applied_at=_now(),
    )


def _clear_flow(flow, authorization_id: int | None, status: str) -> None:
    flow.status = status
    flow.authorization_id = authorization_id
    flow.temporary_session_ciphertext = None
    flow.phone_code_hash_ciphertext = None
    flow.code_preview = None
    flow.flow_version += 1


def _audit_close(session, context: ResumeContext, *, actor, approval_ref, key, classification) -> None:
    audit(
        session, tenant_id=context.operation.tenant_id, actor=actor,
        action=f"收口 SV standby_1 2FA 恢复 {classification}",
        target_type="tg_authorization_dr_operation", target_id=context.operation.id,
        detail=f"approval_ref={approval_ref}; idempotency_key={key}",
    )


__all__ = [
    "INVALID_CLASSIFICATION", "RECOVERY_CLASSIFICATION", "close_invalid", "close_success",
    "idempotent_result", "persist_candidate", "result",
]

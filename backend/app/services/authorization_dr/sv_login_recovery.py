from __future__ import annotations

import hashlib
import json

from sqlalchemy import select

from app.models import (
    AccountProxy,
    AccountStatus,
    AuthorizationDrExecutionNode,
    AuthorizationDrRuntimeContract,
    TelegramDeveloperApp,
    TgAccount,
    TgAccountAuthorization,
    TgAuthorizationDrOperation,
    TgAuthorizationDrReconcileCase,
    TgLoginFlow,
)
from app.security import decrypt_secret, encrypt_secret, encrypt_session
from app.services._common import _now, audit, gateway
from app.services.account_authorization_metadata import resolve_authorization_identity_hash
from app.services.developer_apps import credentials_for_authorization, credentials_for_developer_app
from app.timezone import as_beijing_aware

from .contracts import AuthorizationDrError
from .primary_fence import verified_code_source


RECOVERY_CLASSIFICATION = "sv_login_session_recovered"


def preview_sv_login_recovery(
    session, operation_id: str, *, tenant_id: int, runtime_image_sha: str, requested_by: str,
) -> dict:
    local = _local_inputs(session, operation_id, tenant_id, runtime_image_sha, requested_by)
    remote = _remote_evidence(session, local)
    payload = _evidence_payload(local, remote)
    return {**payload, "evidence_fingerprint": _fingerprint(payload)}


def apply_sv_login_recovery(
    session, operation_id: str, *, tenant_id: int, runtime_image_sha: str,
    requested_by: str, actor: str, approval_ref: str, idempotency_key: str,
    expected_fingerprint: str,
) -> dict:
    _require_approval(requested_by, actor, approval_ref, idempotency_key)
    idempotent = _idempotent_result(session, operation_id, tenant_id, idempotency_key, expected_fingerprint)
    if idempotent:
        return idempotent
    local = _local_inputs(session, operation_id, tenant_id, runtime_image_sha, requested_by)
    remote = _remote_evidence(session, local)
    payload = _evidence_payload(local, remote)
    fingerprint = _fingerprint(payload)
    if fingerprint != expected_fingerprint:
        raise AuthorizationDrError("reconcile_evidence_conflict", "SV login recovery evidence changed")
    locked = _lock_inputs(session, local, payload)
    asset = _persist_recovered_b(session, locked, remote, actor)
    _close_recovery(session, locked, asset, payload, fingerprint, actor, approval_ref, idempotency_key)
    session.commit()
    return _result(locked["operation"], fingerprint)


def readback_sv_login_recovery(session, operation_id: str, tenant_id: int) -> dict:
    operation = session.get(TgAuthorizationDrOperation, operation_id)
    if not operation or operation.tenant_id != tenant_id:
        raise AuthorizationDrError("migration_operation_not_found", "SV login operation does not exist")
    case = session.scalar(select(TgAuthorizationDrReconcileCase).where(
        TgAuthorizationDrReconcileCase.operation_id == operation.id,
    ))
    return {
        "operation_id": operation.id,
        "account_id": operation.account_id,
        "operation_status": operation.status,
        "operation_version": operation.operation_version,
        "candidate_authorization_id": operation.candidate_authorization_id,
        "reconcile_status": operation.reconcile_status,
        "case_status": case.status if case else "missing",
        "classification": case.classification if case else "",
        "evidence_fingerprint": case.evidence_fingerprint if case else "",
    }


def _local_inputs(session, operation_id, tenant_id, runtime_sha, requested_by):
    operation = session.get(TgAuthorizationDrOperation, operation_id)
    if not operation or operation.tenant_id != tenant_id:
        raise AuthorizationDrError("migration_operation_not_found", "SV login operation does not exist")
    account = session.get(TgAccount, operation.account_id)
    primary = verified_code_source(session, operation)
    flow = session.get(TgLoginFlow, operation.login_flow_id)
    app = session.get(TelegramDeveloperApp, operation.developer_app_id)
    proxy_id = int(operation.egress_id.split(":", 1)[1])
    proxy = session.get(AccountProxy, proxy_id)
    conflict = _conflicting_b(session, account, primary)
    _require_recovery_state(session, operation, flow, app, proxy, runtime_sha, requested_by)
    return {
        "operation": operation, "account": account, "primary": primary, "flow": flow,
        "app": app, "proxy": proxy, "conflict": conflict,
        "runtime_image_sha": runtime_sha, "requested_by": requested_by.strip(),
    }


def _require_recovery_state(session, operation, flow, app, proxy, runtime_sha, requested_by):
    contract = session.get(AuthorizationDrRuntimeContract, 1)
    active_clients = sum(session.scalars(select(AuthorizationDrExecutionNode.active_client_count)))
    valid = operation.operation_type == "provision_standby_1"
    valid = valid and operation.status == "reconcile_unknown" and operation.blocker_code == "IntegrityError"
    valid = valid and operation.remote_call_state == "unknown" and operation.candidate_authorization_id is None
    valid = valid and flow and flow.status == AccountStatus.WAITING_CODE.value
    valid = valid and flow.temporary_session_ciphertext and flow.phone_code_hash_ciphertext
    valid = valid and app and app.is_active and proxy and contract and contract.mode == "off"
    valid = valid and active_clients == 0 and runtime_sha and requested_by.strip()
    if not valid:
        raise AuthorizationDrError("reconcile_transition_blocked", "SV login recovery state is not frozen")


def _conflicting_b(session, account, primary):
    rows = list(session.scalars(select(TgAccountAuthorization).where(
        TgAccountAuthorization.account_id == account.id,
        TgAccountAuthorization.logical_slot == "standby_1",
        TgAccountAuthorization.is_slot_current.is_(True),
        TgAccountAuthorization.disabled_at.is_(None),
    )))
    valid = len(rows) == 1 and rows[0].developer_app_id == primary.developer_app_id
    valid = valid and rows[0].protected_from_cleanup and rows[0].status in {"active", "standby"}
    if not valid:
        raise AuthorizationDrError("reconcile_transition_blocked", "Conflicting historical B changed")
    return rows[0]


def _remote_evidence(session, local):
    flow, app, proxy = local["flow"], local["app"], local["proxy"]
    raw_session = decrypt_secret(flow.temporary_session_ciphertext)
    identity = gateway.authorization_identity(raw_session, credentials_for_developer_app(app, proxy))
    identity, hash_source = resolve_authorization_identity_hash(session, local["account"].id, identity)
    if identity.telegram_user_id_digest != local["primary"].telegram_user_id_digest:
        raise AuthorizationDrError("authorization_identity_mismatch", "Recovered B belongs to another account")
    if identity.auth_key_fingerprint_digest == local["primary"].auth_key_fingerprint_digest:
        raise AuthorizationDrError("authorization_identity_mismatch", "Recovered B duplicates A AuthKey")
    remote = gateway.list_authorizations(
        local["primary"].session_ciphertext, credentials_for_authorization(session, local["primary"]),
    )
    if sum(item.api_id == app.api_id for item in remote) != 1:
        raise AuthorizationDrError("reconcile_evidence_conflict", "Recovered App device is not unique")
    return {
        "raw_session": raw_session, "identity": identity, "hash_source": hash_source,
        "remote_set_digest": _remote_set_digest(remote), "remote_device_count": len(remote),
    }


def _evidence_payload(local, remote):
    operation, account, primary = local["operation"], local["account"], local["primary"]
    flow, conflict = local["flow"], local["conflict"]
    identity = remote["identity"]
    return {
        "operation_id": operation.id, "account_id": account.id,
        "operation_version": operation.operation_version, "flow_id": flow.id,
        "flow_version": flow.flow_version, "primary_authorization_id": primary.id,
        "primary_fact_version": primary.fact_version,
        "account_generations": [account.authorization_generation, account.authorization_fact_generation,
                                account.connection_generation],
        "developer_app_id": local["app"].id, "proxy_id": local["proxy"].id,
        "conflicting_authorization_id": conflict.id, "conflicting_fact_version": conflict.fact_version,
        "recovery_session_digest": hashlib.sha256(flow.temporary_session_ciphertext.encode()).hexdigest(),
        "telegram_user_id_digest": identity.telegram_user_id_digest,
        "auth_key_fingerprint_digest": identity.auth_key_fingerprint_digest,
        "authorization_fingerprint_digest": identity.authorization_fingerprint_digest,
        "authorization_hash_source": remote["hash_source"],
        "remote_set_digest": remote["remote_set_digest"],
        "remote_device_count": remote["remote_device_count"],
        "runtime_image_sha": local["runtime_image_sha"], "requested_by": local["requested_by"],
    }


def _lock_inputs(session, local, payload):
    keys = {
        "operation": (TgAuthorizationDrOperation, local["operation"].id),
        "account": (TgAccount, local["account"].id),
        "primary": (TgAccountAuthorization, local["primary"].id),
        "flow": (TgLoginFlow, local["flow"].id),
        "conflict": (TgAccountAuthorization, local["conflict"].id),
    }
    locked = {name: session.scalar(select(model).where(model.id == row_id).with_for_update())
              for name, (model, row_id) in keys.items()}
    _require_locked_state(locked, payload)
    current = _locked_payload(locked, local, payload)
    if current != payload:
        raise AuthorizationDrError("authorization_version_conflict", "SV login recovery facts changed")
    locked.update({"app": local["app"], "proxy": local["proxy"]})
    return locked


def _require_locked_state(locked, payload):
    operation, account, primary = locked["operation"], locked["account"], locked["primary"]
    flow, conflict = locked["flow"], locked["conflict"]
    valid = operation and operation.status == "reconcile_unknown" and operation.blocker_code == "IntegrityError"
    valid = valid and operation.candidate_authorization_id is None and operation.remote_call_state == "unknown"
    valid = valid and account and account.current_authorization_id == primary.id == payload["primary_authorization_id"]
    valid = valid and flow and flow.status == AccountStatus.WAITING_CODE.value
    valid = valid and flow.temporary_session_ciphertext and flow.phone_code_hash_ciphertext
    valid = valid and conflict and conflict.is_slot_current and conflict.logical_slot == "standby_1"
    valid = valid and conflict.developer_app_id == primary.developer_app_id and conflict.protected_from_cleanup
    if not valid:
        raise AuthorizationDrError("authorization_version_conflict", "SV login recovery state changed")


def _locked_payload(locked, local, payload):
    operation, account, primary = locked["operation"], locked["account"], locked["primary"]
    flow, conflict = locked["flow"], locked["conflict"]
    current = dict(payload)
    current.update({
        "operation_version": operation.operation_version, "flow_version": flow.flow_version,
        "primary_fact_version": primary.fact_version,
        "account_generations": [account.authorization_generation, account.authorization_fact_generation,
                                account.connection_generation],
        "conflicting_fact_version": conflict.fact_version,
        "recovery_session_digest": hashlib.sha256(flow.temporary_session_ciphertext.encode()).hexdigest(),
    })
    return current


def _persist_recovered_b(session, locked, remote, actor):
    conflict, operation = locked["conflict"], locked["operation"]
    conflict.role = "standby_repair"
    conflict.logical_slot = "standby_repair"
    conflict.is_slot_current = False
    conflict.status = "needs_repair"
    conflict.health_status = "unknown"
    conflict.derived_status = "needs_repair"
    conflict.protected_from_cleanup = True
    conflict.failure_reason = "Retained after recovered SV standby login"
    conflict.fact_version += 1
    identity = remote["identity"]
    asset = TgAccountAuthorization(
        tenant_id=operation.tenant_id, account_id=operation.account_id, role="standby_1",
        logical_slot="standby_1", is_slot_current=True, provision_region_code="sv",
        developer_app_id=operation.developer_app_id, developer_app_api_id_snapshot=locked["app"].api_id,
        proxy_id=locked["proxy"].id, session_ciphertext=encrypt_session(remote["raw_session"]),
        status="standby", health_status="healthy", derived_status="healthy", is_current=False,
        remote_authorization_state="active", protected_from_cleanup=True, dr_state="dormant_ready",
        telegram_authorization_hash_ciphertext=encrypt_secret(identity.authorization_hash),
        auth_key_fingerprint_digest=identity.auth_key_fingerprint_digest,
        telegram_user_id_digest=identity.telegram_user_id_digest,
        telegram_login_at=as_beijing_aware(locked["flow"].challenge_sent_at),
        last_health_check_at=_now(), last_success_at=_now(), fact_version=2, created_by=actor,
    )
    session.add(asset)
    session.flush()
    return asset


def _close_recovery(session, locked, asset, payload, fingerprint, actor, approval_ref, key):
    operation, flow = locked["operation"], locked["flow"]
    case = TgAuthorizationDrReconcileCase(
        tenant_id=operation.tenant_id, account_id=operation.account_id, operation_id=operation.id,
        status="applied", classification=RECOVERY_CLASSIFICATION, recommended_transition="succeeded",
        blocker_code="IntegrityError", expected_operation_version=payload["operation_version"],
        expected_item_version=0, expected_source_fact_version=payload["primary_fact_version"],
        expected_owner_epoch=operation.owner_epoch, expected_node_id="sv",
        expected_runtime_image_sha=payload["runtime_image_sha"], evidence_fingerprint=fingerprint,
        evidence_manifest=payload, persisted_artifact_state="central_candidate_committed",
        requested_by=payload["requested_by"], applied_by=actor, approval_ref=approval_ref,
        apply_idempotency_key=key, applied_at=_now(),
    )
    session.add(case)
    session.flush()
    operation.candidate_authorization_id = asset.id
    operation.status = "succeeded"
    operation.blocker_code = ""
    operation.remote_call_state = "succeeded"
    operation.reconcile_case_id = case.id
    operation.reconcile_status = "applied"
    operation.reconciled_at = _now()
    operation.finished_at = _now()
    operation.operation_version += 1
    flow.status = AccountStatus.ACTIVE.value
    flow.authorization_id = asset.id
    flow.temporary_session_ciphertext = None
    flow.phone_code_hash_ciphertext = None
    flow.code_preview = None
    audit(session, tenant_id=operation.tenant_id, actor=actor, action="恢复已登录的 SV standby_1",
          target_type="tg_authorization_dr_operation", target_id=operation.id,
          detail=f"approval_ref={approval_ref}; idempotency_key={key}; authorization_id={asset.id}")


def _idempotent_result(session, operation_id, tenant_id, key, fingerprint):
    operation = session.get(TgAuthorizationDrOperation, operation_id)
    case = session.scalar(select(TgAuthorizationDrReconcileCase).where(
        TgAuthorizationDrReconcileCase.operation_id == operation_id,
    ))
    if not operation or operation.tenant_id != tenant_id or not case:
        return None
    if operation.status != "succeeded" or case.classification != RECOVERY_CLASSIFICATION:
        return None
    if case.apply_idempotency_key != key or case.evidence_fingerprint != fingerprint:
        raise AuthorizationDrError("reconcile_evidence_conflict", "SV login recovery idempotency changed")
    return _result(operation, fingerprint)


def _require_approval(requested_by, actor, approval_ref, key):
    valid = requested_by.strip() and actor.strip() and requested_by.strip() != actor.strip()
    if not valid or not approval_ref.strip() or not key.strip():
        raise AuthorizationDrError("reconcile_approval_required", "SV login recovery approval is incomplete")


def _remote_set_digest(rows) -> str:
    payload = sorted([
        [row.authorization_hash, row.is_current, row.api_id, row.device_model, row.platform,
         str(row.date_created), str(row.date_active)] for row in rows
    ])
    return hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()


def _fingerprint(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _result(operation, fingerprint):
    return {
        "operation_id": operation.id, "account_id": operation.account_id,
        "operation_status": operation.status, "operation_version": operation.operation_version,
        "candidate_authorization_id": operation.candidate_authorization_id,
        "reconcile_status": operation.reconcile_status, "evidence_fingerprint": fingerprint,
    }


__all__ = ["apply_sv_login_recovery", "preview_sv_login_recovery", "readback_sv_login_recovery"]

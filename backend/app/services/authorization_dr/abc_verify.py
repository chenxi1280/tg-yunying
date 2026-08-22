from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from sqlalchemy import func, select

from app.models import (
    AuthorizationDrRuntimeContract,
    TgAccount,
    TgAccountAuthorization,
    TgAuthorizationDrOperation,
    TgAuthorizationRestoreProbeFact,
    TgAuthorizationWakeBundle,
    TgAuthorizationWakeBundleCopy,
)
from app.security import decrypt_session
from app.services._common import _now, audit, gateway
from app.services.account_authorization_metadata import resolve_authorization_identity_hash
from app.services.developer_apps import credentials_for_authorization

from .contracts import AuthorizationDrError
from .primary_fence import require_primary_code_source, verified_code_source
from .readiness import require_migration_readiness


@dataclass(frozen=True)
class E4Facts:
    account: object
    primary: object
    standby: object
    malaysia: object
    bundle: object
    copies: int
    probe: object
    contract: object
    node: object


@dataclass(frozen=True)
class Approval:
    requested_by: str
    approved_by: str
    approval_ref: str


def preview_abc_e4(session, tenant_id: int, account_id: int, *, idempotency_key: str) -> dict:
    if not idempotency_key.strip():
        raise AuthorizationDrError("idempotency_key_required", "ABC E4 idempotency key is required")
    facts = _e4_facts(session, tenant_id, account_id)
    payload = _preview_payload(facts, idempotency_key)
    return {**payload, "fingerprint": _fingerprint(payload)}


def apply_abc_e4(
    session,
    tenant_id: int,
    account_id: int,
    *,
    idempotency_key: str,
    expected_fingerprint: str,
    requested_by: str,
    approved_by: str,
    approval_ref: str,
) -> dict:
    _require_approval(requested_by, approved_by, approval_ref)
    existing = _operation_by_key(session, tenant_id, idempotency_key)
    if existing:
        return _existing_result(existing, expected_fingerprint)
    preview = preview_abc_e4(session, tenant_id, account_id, idempotency_key=idempotency_key)
    if preview["fingerprint"] != expected_fingerprint:
        raise AuthorizationDrError("migration_fingerprint_conflict", "ABC E4 preview changed")
    account = _account(session, tenant_id, account_id)
    primary = require_primary_code_source(account)
    approval = Approval(requested_by, approved_by, approval_ref)
    operation = _new_operation(account, primary, preview=preview, approval=approval)
    session.add(operation)
    session.commit()
    return _execute_verification(session, operation)


def _execute_verification(session, operation) -> dict:
    primary = verified_code_source(session, operation)
    operation.status = "send_remote_started"
    operation.remote_call_state = "started"
    operation.remote_effect_started_at = _now()
    operation.operation_version += 1
    session.commit()
    marker = f"ABC-CANARY-E4 account={operation.account_id} operation={operation.id}"
    try:
        result = gateway.send_message(
            operation.account_id,
            0,
            marker,
            [],
            primary.session_ciphertext,
            "me",
            credentials_for_authorization(session, primary),
        )
    except Exception as exc:
        _mark_unknown(session, operation, type(exc).__name__)
        raise
    if not result.ok or not result.remote_message_id:
        return _record_send_failure(session, operation, result)
    return _finish_verification(session, operation, str(result.remote_message_id))


def _finish_verification(session, operation, remote_message_id: str) -> dict:
    try:
        primary = verified_code_source(session, operation)
        account = _account(session, operation.tenant_id, operation.account_id)
        standby = _standby_b(session, account, primary)
        _standby_c(session, account, primary)
        _runtime_readiness(session)
        primary_identity = _identity(session, primary)
        standby_identity = _identity(session, standby)
        _validate_remote_identities(
            primary,
            standby,
            primary_identity=primary_identity,
            standby_identity=standby_identity,
        )
    except Exception as exc:
        _mark_manual(
            session,
            operation,
            code=type(exc).__name__,
            remote_message_id=remote_message_id,
        )
        raise
    operation.status = "succeeded"
    operation.remote_call_state = "succeeded"
    operation.finished_at = _now()
    operation.operation_version += 1
    audit(
        session,
        tenant_id=operation.tenant_id,
        actor=operation.approved_by,
        action="完成 ABC canary E4",
        target_type="tg_authorization_dr_operation",
        target_id=operation.id,
        detail=f"approval_ref={operation.approval_ref}; primary_saved_message_id={remote_message_id}",
    )
    session.commit()
    return _result(operation, remote_message_id)


def _account(session, tenant_id: int, account_id: int) -> TgAccount:
    account = session.get(TgAccount, account_id)
    if not account or account.tenant_id != tenant_id or account.deleted_at is not None:
        raise AuthorizationDrError("account_not_found", "ABC E4 account is unavailable")
    return account


def _e4_facts(session, tenant_id: int, account_id: int) -> E4Facts:
    account = _account(session, tenant_id, account_id)
    primary = require_primary_code_source(account)
    standby = _standby_b(session, account, primary)
    malaysia, bundle, copies, probe = _standby_c(session, account, primary)
    if malaysia.auth_key_fingerprint_digest == standby.auth_key_fingerprint_digest:
        raise AuthorizationDrError("authorization_identity_mismatch", "B and C AuthKeys are not independent")
    contract, node = _runtime_readiness(session)
    return E4Facts(account, primary, standby, malaysia, bundle, copies, probe, contract, node)


def _standby_b(session, account, primary) -> TgAccountAuthorization:
    row = session.scalar(select(TgAccountAuthorization).where(
        TgAccountAuthorization.account_id == account.id,
        TgAccountAuthorization.logical_slot == "standby_1",
        TgAccountAuthorization.is_slot_current.is_(True),
        TgAccountAuthorization.disabled_at.is_(None),
    ))
    valid = (
        row
        and row.provision_region_code == "sv"
        and row.health_status == "healthy"
        and row.session_ciphertext
    )
    if not valid or row.telegram_user_id_digest != primary.telegram_user_id_digest:
        raise AuthorizationDrError("sv_redundancy_incomplete", "Qualified B is unavailable")
    if not row.auth_key_fingerprint_digest or row.auth_key_fingerprint_digest == primary.auth_key_fingerprint_digest:
        raise AuthorizationDrError("authorization_identity_mismatch", "B AuthKey is not independent")
    return row


def _standby_c(session, account, primary):
    row = session.scalar(select(TgAccountAuthorization).where(
        TgAccountAuthorization.account_id == account.id,
        TgAccountAuthorization.logical_slot == "standby_2",
        TgAccountAuthorization.is_slot_current.is_(True),
        TgAccountAuthorization.disabled_at.is_(None),
    ))
    valid = (
        row
        and row.provision_region_code == "my"
        and row.health_status == "healthy"
        and not row.is_current
    )
    if not valid or row.telegram_user_id_digest != primary.telegram_user_id_digest:
        raise AuthorizationDrError("migration_artifact_incomplete", "Qualified C is unavailable")
    if not row.auth_key_fingerprint_digest or row.auth_key_fingerprint_digest == primary.auth_key_fingerprint_digest:
        raise AuthorizationDrError("authorization_identity_mismatch", "C AuthKey is not independent")
    bundle = session.get(TgAuthorizationWakeBundle, row.wake_bundle_id) if row.wake_bundle_id else None
    copies = _copy_count(session, bundle.id) if bundle else 0
    probe = _restore_probe(session, bundle.id) if bundle else None
    bundle_ready = (
        bundle
        and bundle.is_active
        and bundle.receipt_status == "active"
        and bundle.kms_decrypt_status == "passed"
        and bundle.protected_from_cleanup
        and bundle.recoverable_copy_count == 2
        and bundle.auth_key_fingerprint_digest == row.auth_key_fingerprint_digest
        and bundle.telegram_user_id_digest == row.telegram_user_id_digest
        and copies == 2
    )
    if not bundle_ready:
        raise AuthorizationDrError("migration_artifact_incomplete", "C does not have two recoverable copies")
    probe_ready = (
        probe
        and probe.status == "passed"
        and probe.session_parse_status == "passed"
        and probe.authorization_status == "passed"
        and probe.identity_match_status == "passed"
        and probe.auth_key_match_status == "passed"
        and probe.source_client_disconnected
        and probe.probe_client_disconnected
    )
    if not probe_ready:
        raise AuthorizationDrError("restore_probe_failed", "C restore probe is incomplete")
    return row, bundle, copies, probe


def _runtime_readiness(session):
    contract = session.get(AuthorizationDrRuntimeContract, 1)
    if not contract or contract.mode != "off" or contract.claim_scope_operation_id:
        raise AuthorizationDrError("runtime_capability_unproven", "DR runtime is not safely off")
    readiness = require_migration_readiness(session, require_mode=False)
    return contract, readiness.node


def _copy_count(session, bundle_id: str) -> int:
    return int(session.scalar(select(func.count()).select_from(TgAuthorizationWakeBundleCopy).where(
        TgAuthorizationWakeBundleCopy.bundle_id == bundle_id,
    )) or 0)


def _restore_probe(session, bundle_id: str):
    return session.scalar(select(TgAuthorizationRestoreProbeFact).where(
        TgAuthorizationRestoreProbeFact.bundle_id == bundle_id,
    ).order_by(TgAuthorizationRestoreProbeFact.probe_generation.desc()))


def _preview_payload(facts: E4Facts, key: str) -> dict:
    return {
        "tenant_id": facts.account.tenant_id,
        "account_id": facts.account.id,
        "idempotency_key": key.strip(),
        "primary": [facts.primary.id, facts.primary.fact_version, facts.primary.auth_key_fingerprint_digest],
        "standby_1": [facts.standby.id, facts.standby.fact_version, facts.standby.auth_key_fingerprint_digest],
        "standby_2": [facts.malaysia.id, facts.malaysia.fact_version, facts.malaysia.auth_key_fingerprint_digest],
        "account_generations": _account_generations(facts.account),
        "bundle": [facts.bundle.id, facts.bundle.bundle_generation, facts.copies, facts.probe.id],
        "runtime": _runtime_fingerprint(facts.contract, facts.node),
    }


def _account_generations(account) -> list[int]:
    return [
        account.authorization_generation,
        account.authorization_fact_generation,
        account.connection_generation,
    ]


def _runtime_fingerprint(contract, node) -> list:
    return [contract.id, contract.version, contract.mode, node.id, node.version, node.runtime_image_sha]


def _new_operation(account, primary, *, preview, approval: Approval):
    return TgAuthorizationDrOperation(
        tenant_id=account.tenant_id,
        account_id=account.id,
        operation_type="abc_e4_primary_send",
        logical_slot="primary",
        source_authorization_id=primary.id,
        code_source_authorization_id=primary.id,
        source_generation=primary.slot_generation,
        target_generation=primary.slot_generation,
        expected_current_authorization_id=primary.id,
        expected_authorization_generation=account.authorization_generation,
        expected_authorization_fact_generation=account.authorization_fact_generation,
        expected_connection_generation=account.connection_generation,
        expected_code_source_fact_version=primary.fact_version,
        expected_code_source_user_id_digest=primary.telegram_user_id_digest,
        expected_code_source_auth_key_digest=primary.auth_key_fingerprint_digest,
        developer_app_id=primary.developer_app_id,
        developer_app_api_id_snapshot=primary.developer_app_api_id_snapshot,
        developer_app_credentials_version=primary.developer_app.credentials_version,
        assignment_version=1,
        egress_id=f"sv-proxy:{primary.proxy_id or 0}",
        egress_version=1,
        idempotency_key=preview["idempotency_key"],
        request_fingerprint=preview["fingerprint"],
        status="approved",
        requested_by=approval.requested_by,
        approved_by=approval.approved_by,
        approval_ref=approval.approval_ref,
    )


def _identity(session, authorization):
    identity = gateway.authorization_identity(
        decrypt_session(authorization.session_ciphertext),
        credentials_for_authorization(session, authorization),
    )
    identity, _hash_source = resolve_authorization_identity_hash(
        session,
        authorization.account_id,
        identity,
        exclude_authorization_id=authorization.id,
    )
    return identity


def _validate_remote_identities(primary, standby, *, primary_identity, standby_identity) -> None:
    if not primary_identity.authorization_hash or primary_identity.authorization_hash == "0":
        raise AuthorizationDrError("authorization_hash_missing", "A Telegram authorization hash is missing")
    if not standby_identity.authorization_hash or standby_identity.authorization_hash == "0":
        raise AuthorizationDrError("authorization_hash_missing", "B Telegram authorization hash is missing")
    if primary_identity.telegram_user_id_digest != primary.telegram_user_id_digest:
        raise AuthorizationDrError("authorization_identity_mismatch", "A Telegram identity changed")
    if primary_identity.auth_key_fingerprint_digest != primary.auth_key_fingerprint_digest:
        raise AuthorizationDrError("authorization_identity_mismatch", "A AuthKey changed")
    if standby_identity.telegram_user_id_digest != primary.telegram_user_id_digest:
        raise AuthorizationDrError("authorization_identity_mismatch", "B Telegram identity changed")
    if standby_identity.auth_key_fingerprint_digest != standby.auth_key_fingerprint_digest:
        raise AuthorizationDrError("authorization_identity_mismatch", "B AuthKey changed")


def _record_send_failure(session, operation, result) -> dict:
    code = str(result.failure_type or "primary_saved_message_failed")
    if result.remote_mutation_started is False:
        operation.status = "failed"
        operation.remote_call_state = "failed"
        operation.finished_at = _now()
        operation.blocker_code = code[:100]
        session.commit()
        return _result(operation, "")
    _mark_unknown(session, operation, code)
    return _result(operation, "")


def _mark_unknown(session, operation, code: str) -> None:
    operation.status = "reconcile_unknown"
    operation.remote_call_state = "unknown"
    operation.blocker_code = code[:100]
    operation.operation_version += 1
    session.commit()


def _mark_manual(session, operation, *, code: str, remote_message_id: str) -> None:
    session.rollback()
    operation = session.get(TgAuthorizationDrOperation, operation.id)
    operation.status = "manual_required"
    operation.remote_call_state = "succeeded"
    operation.blocker_code = code[:100]
    operation.finished_at = _now()
    operation.operation_version += 1
    audit(session, tenant_id=operation.tenant_id, actor=operation.approved_by, action="ABC E4 发送后读回失败",
          target_type="tg_authorization_dr_operation", target_id=operation.id,
          detail=f"primary_saved_message_id={remote_message_id}; blocker={code[:100]}")
    session.commit()


def _operation_by_key(session, tenant_id: int, key: str):
    return session.scalar(select(TgAuthorizationDrOperation).where(
        TgAuthorizationDrOperation.tenant_id == tenant_id,
        TgAuthorizationDrOperation.idempotency_key == key.strip(),
    ))


def _existing_result(operation, expected_fingerprint: str) -> dict:
    if operation.request_fingerprint != expected_fingerprint:
        raise AuthorizationDrError("migration_fingerprint_conflict", "ABC E4 idempotency key changed")
    return _result(operation, "")


def _result(operation, remote_message_id: str) -> dict:
    return {"operation_id": operation.id, "account_id": operation.account_id, "status": operation.status,
            "blocker_code": operation.blocker_code, "primary_saved_message_id": remote_message_id}


def _require_approval(requested_by: str, approved_by: str, approval_ref: str) -> None:
    if not requested_by.strip() or not approved_by.strip() or not approval_ref.strip():
        raise AuthorizationDrError("approval_ref_required", "Requester, approver and approval ref are required")
    if requested_by.strip() == approved_by.strip():
        raise AuthorizationDrError("approval_actor_conflict", "Approver must differ from requester")


def _fingerprint(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


__all__ = ["apply_abc_e4", "preview_abc_e4"]

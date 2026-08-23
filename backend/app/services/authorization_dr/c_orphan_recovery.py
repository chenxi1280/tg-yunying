from __future__ import annotations

import hashlib
import json
from datetime import timedelta

from sqlalchemy import func, select

from app.models import (
    AuthorizationDrExecutionNode,
    AuthorizationDrRuntimeContract,
    TgAccount,
    TgAccountAuthorization,
    TgAuthorizationDrBatchItem,
    TgAuthorizationDrOperation,
    TgAuthorizationDrStageFact,
    TgAuthorizationOnlineAbcBatch,
    TgAuthorizationOnlineAbcItem,
    TgAuthorizationOnlineAbcSlotResult,
    TgAuthorizationWakeBundle,
)
from app.security import decrypt_secret
from app.services._common import _now, audit, gateway
from app.services.developer_apps import credentials_for_authorization
from app.timezone import as_beijing

from .contracts import AuthorizationDrError
from .stage_facts import append_stage_fact


LOGIN_WINDOW_SECONDS = 300
RECOVERY_BLOCKER = "c_orphan_revoked_retry_ready"
STARTED_STAGE = "orphan_revoke_started"


def preview_c_orphan_recovery(session, batch_id: str, account_id: int) -> dict:
    preview, _candidate_hash = _build_preview(session, batch_id, account_id)
    return preview


def _build_preview(session, batch_id: str, account_id: int):
    facts = _facts(session, batch_id, account_id)
    remote = _remote_authorizations(session, facts)
    candidate = _unique_candidate(facts, remote)
    payload = _payload(facts, remote, candidate)
    return {**payload, "fingerprint": _digest(payload)}, candidate.authorization_hash


def apply_c_orphan_recovery(
    session,
    batch_id: str,
    account_id: int,
    *,
    expected_fingerprint: str,
    requested_by: str,
    approved_by: str,
    approval_ref: str,
) -> dict:
    _require_approval(requested_by, approved_by, approval_ref)
    facts = _facts(session, batch_id, account_id)
    started = _started_fact(session, facts["operation"].id)
    if started:
        return _finish_started(session, facts, started, expected_fingerprint, approved_by, approval_ref)
    preview, candidate_hash = _build_preview(session, batch_id, account_id)
    if preview["fingerprint"] != expected_fingerprint:
        raise AuthorizationDrError("reconcile_evidence_conflict", "C orphan recovery preview changed")
    _persist_started(session, facts, preview, approved_by, approval_ref)
    result = gateway.cleanup_authorization(
        facts["primary"].session_ciphertext,
        candidate_hash,
        credentials_for_authorization(session, facts["primary"]),
    )
    if not result.ok:
        raise AuthorizationDrError("reconcile_unknown", "C orphan revoke outcome is not confirmed")
    return _finish_started(
        session, _facts(session, batch_id, account_id),
        _started_fact(session, facts["operation"].id), expected_fingerprint, approved_by, approval_ref,
    )


def _facts(session, batch_id: str, account_id: int) -> dict:
    _require_runtime_off(session)
    batch = session.get(TgAuthorizationOnlineAbcBatch, batch_id)
    item = session.scalar(select(TgAuthorizationOnlineAbcItem).where(
        TgAuthorizationOnlineAbcItem.batch_id == batch_id,
        TgAuthorizationOnlineAbcItem.account_id == account_id,
    )) if batch else None
    slot = session.scalar(select(TgAuthorizationOnlineAbcSlotResult).where(
        TgAuthorizationOnlineAbcSlotResult.item_id == item.id,
        TgAuthorizationOnlineAbcSlotResult.logical_slot == "standby_2",
    )) if item else None
    operation = session.get(TgAuthorizationDrOperation, slot.operation_id) if slot and slot.operation_id else None
    account = session.get(TgAccount, account_id)
    primary = session.get(TgAccountAuthorization, item.primary_authorization_id) if item else None
    _require_frozen(session, batch, item, slot, operation, account, primary)
    return {"session": session, "batch": batch, "item": item, "slot": slot, "operation": operation,
            "account": account, "primary": primary}


def _require_runtime_off(session) -> None:
    contract = session.get(AuthorizationDrRuntimeContract, 1)
    clients = session.scalar(select(func.sum(AuthorizationDrExecutionNode.active_client_count)))
    if not contract or contract.mode != "off" or contract.claim_scope_operation_id or int(clients or 0):
        raise AuthorizationDrError("reconcile_runtime_conflict", "DR runtime and clients must be stopped")


def _require_frozen(session, batch, item, slot, operation, account, primary) -> None:
    account_delta = account.authorization_fact_generation - item.authorization_fact_generation if account else -1
    primary_delta = primary.fact_version - item.primary_fact_version if primary else -1
    valid = batch and batch.status == "stopped" and item and slot and operation and account and primary
    valid = valid and item.status == "stopped" and item.outcome == "reconcile_unknown"
    valid = valid and slot.outcome == "reconcile_unknown"
    valid = valid and operation.status == "provision_reconcile_unknown"
    valid = valid and operation.remote_call_state == "unknown" and operation.candidate_authorization_id is None
    valid = valid and operation.remote_effect_started_at and _confirmed_stage(session, operation, item)
    valid = valid and account.current_authorization_id == primary.id and primary.is_current
    valid = valid and account.authorization_generation == item.authorization_generation
    valid = valid and account.connection_generation == item.connection_generation
    valid = valid and account_delta == primary_delta and account_delta >= 1
    valid = valid and primary.status == "active" and primary.health_status == "healthy"
    valid = valid and primary.last_authoritative_error_code == "" and primary.disabled_at is None
    valid = valid and primary.telegram_user_id_digest == operation.expected_code_source_user_id_digest
    valid = valid and primary.auth_key_fingerprint_digest == operation.expected_code_source_auth_key_digest
    if not valid:
        raise AuthorizationDrError("reconcile_frozen_fact_conflict", "C orphan recovery facts changed")


def _confirmed_stage(session, operation, item) -> bool:
    fact = session.scalar(select(TgAuthorizationDrStageFact.id).where(
        TgAuthorizationDrStageFact.operation_id == operation.id,
        TgAuthorizationDrStageFact.stage == "remote_login_confirmed",
    ))
    return bool(operation.account_id == item.account_id and operation.login_code_received_at and fact)


def _remote_authorizations(session, facts):
    primary = facts["primary"]
    return gateway.list_authorizations(
        primary.session_ciphertext, credentials_for_authorization(session, primary),
    )


def _unique_candidate(facts, remote):
    operation = facts["operation"]
    known = _known_hashes(facts, operation.account_id)
    started = as_beijing(operation.remote_effect_started_at)
    lower = started - timedelta(seconds=5)
    upper = started + timedelta(seconds=LOGIN_WINDOW_SECONDS)
    matches = [row for row in remote if _candidate_matches(row, operation, known, lower, upper)]
    if len(matches) != 1:
        raise AuthorizationDrError("reconcile_evidence_conflict", "C orphan candidate is not unique")
    return matches[0]


def _known_hashes(facts, account_id: int) -> set[str]:
    session = facts["session"]
    rows = session.scalars(select(TgAccountAuthorization).where(
        TgAccountAuthorization.account_id == account_id,
    ))
    return {value for row in rows if (value := _raw_hash(row)) not in {"", "0"}}


def _raw_hash(row) -> str:
    value = row.telegram_authorization_hash_ciphertext or ""
    return str(decrypt_secret(value) or value) if value else ""


def _candidate_matches(row, operation, known, lower, upper) -> bool:
    created = as_beijing(row.date_created)
    return bool(
        not row.is_current and row.authorization_hash not in known
        and row.api_id == operation.developer_app_api_id_snapshot
        and created and lower <= created <= upper
    )


def _payload(facts, remote, candidate) -> dict:
    operation, item, account = facts["operation"], facts["item"], facts["account"]
    return {
        "batch_id": facts["batch"].id, "item_id": item.id, "account_id": item.account_id,
        "operation_id": operation.id, "operation_version": operation.operation_version,
        "item_version": item.version, "account_generations": [
            account.authorization_generation, account.authorization_fact_generation,
            account.connection_generation,
        ],
        "candidate_hash_digest": _digest(candidate.authorization_hash),
        "remote_set_digest": _remote_digest(remote),
    }


def _persist_started(session, facts, preview, actor, approval_ref) -> None:
    operation = facts["operation"]
    manifest = {
        "fingerprint": preview["fingerprint"],
        "candidate_hash_digest": preview["candidate_hash_digest"],
        "remote_set_digest": preview["remote_set_digest"],
    }
    append_stage_fact(
        session, operation, stage=STARTED_STAGE,
        manifest_digest=preview["fingerprint"], evidence_manifest=manifest,
    )
    audit(session, tenant_id=operation.tenant_id, actor=actor, action="开始精确撤销 C orphan",
          target_type="tg_authorization_dr_operation", target_id=operation.id,
          detail=f"approval_ref={approval_ref}; fingerprint={preview['fingerprint']}")
    session.commit()


def _finish_started(session, facts, started, fingerprint, actor, approval_ref) -> dict:
    if not started or started.manifest_digest != fingerprint:
        raise AuthorizationDrError("reconcile_evidence_conflict", "C orphan recovery intent changed")
    candidate_digest = dict(started.evidence_manifest)["candidate_hash_digest"]
    remote = _remote_authorizations(session, facts)
    if any(_digest(row.authorization_hash) == candidate_digest for row in remote):
        raise AuthorizationDrError("reconcile_unknown", "C orphan is still present after revoke")
    return _finish_compensation(session, facts, fingerprint, actor, approval_ref)


def _finish_compensation(session, facts, fingerprint, actor, approval_ref) -> dict:
    operation, item, slot, batch = (
        facts["operation"], facts["item"], facts["slot"], facts["batch"],
    )
    migration_item = (
        session.get(TgAuthorizationDrBatchItem, operation.batch_item_id)
        if operation.batch_item_id else None
    )
    operation.status = "migration_rolled_back_forward"
    operation.remote_call_state = "compensated"
    operation.reconcile_status = "applied"
    operation.blocker_code = "orphan_remote_authorization_revoked"
    operation.reconciled_at = _now(); operation.finished_at = _now(); operation.operation_version += 1
    if migration_item:
        migration_item.status = operation.status; migration_item.outcome = operation.status
        migration_item.blocker_code = operation.blocker_code; migration_item.finished_at = _now()
        migration_item.version += 1
    item.status = "stopped"; item.outcome = "runner_blocked"; item.blocker_code = RECOVERY_BLOCKER
    item.finished_at = _now(); item.version += 1
    slot.operation_id = None; slot.outcome = "pending"; slot.blocker_code = ""; slot.version += 1
    batch.status = "stopped"; batch.version += 1
    audit(session, tenant_id=operation.tenant_id, actor=actor, action="完成 C orphan 撤销并开放同 item 重试",
          target_type="tg_authorization_dr_operation", target_id=operation.id,
          detail=f"approval_ref={approval_ref}; fingerprint={fingerprint}")
    session.commit()
    return {"operation_id": operation.id, "operation_status": operation.status,
            "account_id": operation.account_id, "item_status": item.status,
            "item_blocker": item.blocker_code, "fingerprint": fingerprint}


def _started_fact(session, operation_id: str):
    return session.scalar(select(TgAuthorizationDrStageFact).where(
        TgAuthorizationDrStageFact.operation_id == operation_id,
        TgAuthorizationDrStageFact.stage == STARTED_STAGE,
    ))


def _remote_digest(rows) -> str:
    payload = sorted([[row.authorization_hash, row.is_current, row.api_id, str(row.date_created)] for row in rows])
    return _digest(payload)


def _digest(value) -> str:
    encoded = value if isinstance(value, str) else json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _require_approval(requested_by: str, approved_by: str, approval_ref: str) -> None:
    valid = requested_by.strip() and approved_by.strip() and approval_ref.strip()
    if not valid or requested_by.strip() == approved_by.strip():
        raise AuthorizationDrError("reconcile_approval_required", "C orphan recovery requires dual approval")


__all__ = ["apply_c_orphan_recovery", "preview_c_orphan_recovery"]

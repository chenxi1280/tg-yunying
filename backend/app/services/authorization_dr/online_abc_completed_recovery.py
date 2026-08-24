from __future__ import annotations

from sqlalchemy import select

from app.models import (
    AuditLog,
    TgAccount,
    TgAccountAuthorization,
    TgAuthorizationDrOperation,
    TgAuthorizationLocalActivateCase,
    TgAuthorizationOnlineAbcBatch,
    TgAuthorizationOnlineAbcItem,
)
from app.services._common import _now, audit

from . import online_abc_post_activate as base
from .abc_backup import preview_abc_backup
from .contracts import AuthorizationDrError
from .online_abc_operations import online_abc_item_operations, online_abc_operation_keys


COMPLETED_REBASE_BLOCKER = "post_completed_local_activate_rebase_ready"
COMPLETED_AUDIT_ACTION = "重绑 completed local_activate 后 ABC item"
REARM_AUDIT_ACTION = "恢复 pre-remote ABC rebase checkpoint"
PRIMARY_DRIFT_OUTCOME = "primary_drift_after_success"


def preview_completed_rebase(
    session, batch_id: str, account_id: int, case_id: str, *, idempotency_key: str,
) -> dict:
    batch, item, case = _inputs(session, batch_id, account_id, case_id)
    body = _completed_body(session, batch, item, case, idempotency_key)
    return {**body, "fingerprint": base._fingerprint(body)}


def apply_completed_rebase(
    session,
    batch_id: str,
    account_id: int,
    case_id: str,
    *,
    idempotency_key: str,
    expected_fingerprint: str,
    requested_by: str,
    approved_by: str,
    approval_ref: str,
) -> dict:
    base._require_approval(requested_by, approved_by, approval_ref)
    batch, item = base._locked_batch_item(session, batch_id, account_id)
    existing = _existing_audit(session, item.id, idempotency_key, COMPLETED_AUDIT_ACTION)
    if existing:
        base._require_existing_fingerprint(existing, expected_fingerprint)
        return _result(item, case_id, expected_fingerprint, already_applied=True)
    base._require_batch_approval(batch, requested_by, approved_by, approval_ref)
    preview = preview_completed_rebase(
        session, batch_id, account_id, case_id, idempotency_key=idempotency_key,
    )
    if preview["fingerprint"] != expected_fingerprint:
        raise AuthorizationDrError("migration_fingerprint_conflict", "Completed rebase preview changed")
    _archive_historical_b(session, preview)
    slot = base._b_slot(session, item.id)
    base._apply_item_projection(item, slot, preview)
    item.status = "stopped"
    item.outcome = "runner_blocked"
    item.blocker_code = COMPLETED_REBASE_BLOCKER
    item.finished_at = _now()
    _audit_recovery(
        session, batch, item, case_id, idempotency_key, expected_fingerprint,
        approved_by, approval_ref, COMPLETED_AUDIT_ACTION,
    )
    session.commit()
    return _result(item, case_id, expected_fingerprint, already_applied=False)


def require_completed_rebase_resume(session, item, operations: dict) -> None:
    base._require_runtime_and_unknown(session)
    audit_row = base._latest_action_audit(session, item.id, COMPLETED_AUDIT_ACTION)
    case_id = base._audit_value(audit_row, "case_id") if audit_row else ""
    case = session.get(TgAuthorizationLocalActivateCase, case_id) if case_id else None
    batch = session.get(TgAuthorizationOnlineAbcBatch, item.batch_id)
    account = session.get(TgAccount, item.account_id)
    primary = session.get(TgAccountAuthorization, item.primary_authorization_id)
    valid = (
        batch
        and batch.status == "stopped"
        and item.status == "stopped"
        and item.outcome == "runner_blocked"
        and item.blocker_code == COMPLETED_REBASE_BLOCKER
        and item.standby_1_plan == "provision"
        and case
        and case.status == "applied"
        and case.target_authorization_id == item.primary_authorization_id
    )
    if not valid:
        raise AuthorizationDrError("online_abc_resume_blocker_forbidden", "Completed rebase changed")
    base._require_primary_pair(session, item, case, account, primary)
    base._require_primary_baseline(item, account, primary)
    _require_completed_operations(session, item, case, operations)
    slot = base._b_slot(session, item.id)
    if slot.outcome != "pending" or slot.operation_id is not None or slot.blocker_code:
        raise AuthorizationDrError("online_abc_resume_remote_effect_started", "Completed B slot changed")
    backup = _backup_preview(session, batch, item)
    if base._backup_projection(backup) != base._item_backup_projection(item):
        raise AuthorizationDrError("online_abc_primary_drift", "Completed B route changed")


def preview_pre_remote_rearm(
    session, batch_id: str, account_id: int, case_id: str, *, idempotency_key: str,
) -> dict:
    batch, item, case = _inputs(session, batch_id, account_id, case_id)
    body = _rearm_body(session, batch, item, case, idempotency_key)
    return {**body, "fingerprint": base._fingerprint(body)}


def apply_pre_remote_rearm(
    session,
    batch_id: str,
    account_id: int,
    case_id: str,
    *,
    idempotency_key: str,
    expected_fingerprint: str,
    requested_by: str,
    approved_by: str,
    approval_ref: str,
) -> dict:
    base._require_approval(requested_by, approved_by, approval_ref)
    batch, item = base._locked_batch_item(session, batch_id, account_id)
    existing = _existing_audit(session, item.id, idempotency_key, REARM_AUDIT_ACTION)
    if existing:
        base._require_existing_fingerprint(existing, expected_fingerprint)
        return _result(item, case_id, expected_fingerprint, already_applied=True)
    base._require_batch_approval(batch, requested_by, approved_by, approval_ref)
    preview = preview_pre_remote_rearm(
        session, batch_id, account_id, case_id, idempotency_key=idempotency_key,
    )
    if preview["fingerprint"] != expected_fingerprint:
        raise AuthorizationDrError("migration_fingerprint_conflict", "Pre-remote rearm preview changed")
    item.status = "stopped"
    item.outcome = "runner_blocked"
    item.blocker_code = base.REBASE_BLOCKER
    item.finished_at = _now()
    item.version += 1
    _audit_recovery(
        session, batch, item, case_id, idempotency_key, expected_fingerprint,
        approved_by, approval_ref, REARM_AUDIT_ACTION,
    )
    session.commit()
    return _result(item, case_id, expected_fingerprint, already_applied=False)


def _completed_body(session, batch, item, case, idempotency_key: str) -> dict:
    key = _key(idempotency_key)
    operations = online_abc_item_operations(session, batch, item)
    account = session.get(TgAccount, item.account_id)
    primary = session.get(TgAccountAuthorization, account.current_authorization_id)
    _require_completed_source(session, batch, item, case, account, primary, operations)
    historical_b = _historical_b_snapshot(operations["b"], case, primary)
    backup = _backup_preview(session, batch, item)
    return {
        "batch_id": batch.id,
        "batch_version": batch.version,
        "item_id": item.id,
        "item_version": item.version,
        "account_id": item.account_id,
        "case_id": case.id,
        "old_primary_authorization_id": case.expected_current_authorization_id,
        "new_primary_authorization_id": primary.id,
        "primary_fact_version": primary.fact_version,
        "authorization_generation": account.authorization_generation,
        "authorization_fact_generation": account.authorization_fact_generation,
        "connection_generation": account.connection_generation,
        "primary_session_digest": base._digest(primary.session_ciphertext or ""),
        "source_c_authorization_id": item.source_c_authorization_id,
        "source_c_fact_version": item.source_c_fact_version,
        "source_c_slot_generation": item.source_c_slot_generation,
        "retained_c_operation_id": operations["c"].id,
        "historical_e4_operation_id": operations["e4"].id,
        "historical_b_operation": historical_b,
        **base._backup_projection(backup),
        "idempotency_key": key,
    }


def _rearm_body(session, batch, item, case, idempotency_key: str) -> dict:
    key = _key(idempotency_key)
    operations = online_abc_item_operations(session, batch, item)
    account = session.get(TgAccount, item.account_id)
    primary = session.get(TgAccountAuthorization, item.primary_authorization_id)
    _require_rearm_source(session, batch, item, case, account, primary, operations)
    return {
        "batch_id": batch.id,
        "batch_version": batch.version,
        "item_id": item.id,
        "item_version": item.version,
        "account_id": item.account_id,
        "case_id": case.id,
        "primary_authorization_id": primary.id,
        "primary_fact_version": primary.fact_version,
        "authorization_generation": account.authorization_generation,
        "authorization_fact_generation": account.authorization_fact_generation,
        "connection_generation": account.connection_generation,
        "primary_session_digest": base._digest(primary.session_ciphertext or ""),
        "compensated_c_operation_id": operations["c"].id,
        "idempotency_key": key,
    }


def _require_completed_source(session, batch, item, case, account, primary, operations) -> None:
    valid = (
        batch.status == "stopped"
        and item.status == "stopped"
        and item.outcome == PRIMARY_DRIFT_OUTCOME
        and item.blocker_code == PRIMARY_DRIFT_OUTCOME
        and item.primary_authorization_id == case.expected_current_authorization_id
        and case.status == "applied"
        and case.target_authorization_id == account.current_authorization_id
    )
    if not valid:
        raise AuthorizationDrError("online_abc_completed_rebase_unavailable", "Completed checkpoint is unavailable")
    base._require_runtime_and_unknown(session)
    base._require_primary_pair(session, item, case, account, primary)
    _require_completed_operations(session, item, case, operations)


def _historical_b_snapshot(operation, case, primary) -> dict:
    if operation is None:
        return _empty_historical_b_snapshot()
    valid = (
        operation.operation_type == "provision_standby_1"
        and operation.status == "succeeded"
        and operation.remote_call_state == "succeeded"
        and operation.blocker_code == ""
        and operation.source_authorization_id == case.expected_current_authorization_id
        and operation.code_source_authorization_id == case.expected_current_authorization_id
        and operation.expected_current_authorization_id == case.expected_current_authorization_id
        and operation.candidate_authorization_id == primary.id
    )
    if not valid:
        raise AuthorizationDrError("online_abc_completed_rebase_unavailable", "Historical B operation changed")
    return {
        "id": operation.id,
        "version": operation.operation_version,
        "idempotency_key": operation.idempotency_key,
        "source_authorization_id": operation.source_authorization_id,
        "code_source_authorization_id": operation.code_source_authorization_id,
        "expected_current_authorization_id": operation.expected_current_authorization_id,
        "candidate_authorization_id": operation.candidate_authorization_id,
        "status": operation.status,
        "remote_call_state": operation.remote_call_state,
        "blocker_code": operation.blocker_code,
    }


def _archive_historical_b(session, preview: dict) -> None:
    frozen = preview["historical_b_operation"]
    if frozen["id"] is None:
        return
    operation = session.scalar(select(TgAuthorizationDrOperation).where(
        TgAuthorizationDrOperation.id == frozen["id"],
    ).with_for_update())
    if not operation or _locked_historical_b_snapshot(operation) != frozen:
        raise AuthorizationDrError("authorization_version_conflict", "Historical B operation changed")
    archived_key = f"hist-b:{operation.id}:{preview['case_id']}"
    conflict = session.scalar(select(TgAuthorizationDrOperation.id).where(
        TgAuthorizationDrOperation.tenant_id == operation.tenant_id,
        TgAuthorizationDrOperation.idempotency_key == archived_key,
        TgAuthorizationDrOperation.id != operation.id,
    ).limit(1))
    if conflict:
        raise AuthorizationDrError("reconcile_idempotency_conflict", "Historical B archive key exists")
    operation.idempotency_key = archived_key
    operation.operation_version += 1


def _empty_historical_b_snapshot() -> dict:
    return {
        "id": None,
        "version": 0,
        "idempotency_key": "",
        "source_authorization_id": None,
        "code_source_authorization_id": None,
        "expected_current_authorization_id": None,
        "candidate_authorization_id": None,
        "status": "",
        "remote_call_state": "",
        "blocker_code": "",
    }


def _locked_historical_b_snapshot(operation) -> dict:
    return {
        "id": operation.id,
        "version": operation.operation_version,
        "idempotency_key": operation.idempotency_key,
        "source_authorization_id": operation.source_authorization_id,
        "code_source_authorization_id": operation.code_source_authorization_id,
        "expected_current_authorization_id": operation.expected_current_authorization_id,
        "candidate_authorization_id": operation.candidate_authorization_id,
        "status": operation.status,
        "remote_call_state": operation.remote_call_state,
        "blocker_code": operation.blocker_code,
    }


def _require_completed_operations(session, item, case, operations) -> None:
    c_operation = operations["c"]
    e4_operation = operations["e4"]
    valid = (
        c_operation
        and c_operation.status == "succeeded"
        and c_operation.candidate_authorization_id
        and c_operation.expected_current_authorization_id == case.expected_current_authorization_id
        and e4_operation
        and e4_operation.status == "succeeded"
        and e4_operation.remote_call_state == "succeeded"
        and e4_operation.expected_current_authorization_id == case.expected_current_authorization_id
    )
    if not valid:
        raise AuthorizationDrError("online_abc_completed_rebase_unavailable", "Completed C/E4 facts changed")
    _require_retained_source_c(session, item)
    candidate = session.get(TgAccountAuthorization, c_operation.candidate_authorization_id)
    if not _ready_c_candidate(candidate, item.account_id):
        raise AuthorizationDrError("migration_source_drift", "Qualified C changed after A activation")


def _require_rearm_source(session, batch, item, case, account, primary, operations) -> None:
    valid = (
        batch.status == "stopped"
        and item.status == "running"
        and item.outcome == "running"
        and item.blocker_code == ""
        and item.standby_1_plan == "provision"
        and operations["b"] is None
        and operations["e4"] is None
        and base._compensated_c(operations["c"])
        and _other_primary_drift(session, item)
    )
    if not valid:
        raise AuthorizationDrError("online_abc_pre_remote_rearm_unavailable", "Pre-remote rearm is unavailable")
    base._require_runtime_and_unknown(session)
    base._require_primary_pair(session, item, case, account, primary)
    base._require_primary_baseline(item, account, primary)
    base._require_source_c(session, item)
    slot = base._b_slot(session, item.id)
    if slot.outcome != "pending" or slot.operation_id is not None or slot.blocker_code:
        raise AuthorizationDrError("online_abc_resume_remote_effect_started", "Pre-remote B slot changed")


def _require_retained_source_c(session, item) -> None:
    source = session.get(TgAccountAuthorization, item.source_c_authorization_id)
    valid = (
        source
        and source.account_id == item.account_id
        and source.fact_version == item.source_c_fact_version
        and source.slot_generation == item.source_c_slot_generation
        and source.logical_slot == "standby_2"
        and source.protected_from_cleanup
    )
    if not valid:
        raise AuthorizationDrError("migration_source_drift", "Retained C source changed")


def _ready_c_candidate(candidate, account_id: int) -> bool:
    return bool(
        candidate
        and candidate.account_id == account_id
        and candidate.logical_slot == "standby_2"
        and candidate.is_slot_current
        and not candidate.is_current
        and candidate.provision_region_code == "my"
        and candidate.status == "standby"
        and candidate.health_status == "healthy"
        and candidate.wake_bundle_id
        and candidate.protected_from_cleanup
    )


def _other_primary_drift(session, item) -> bool:
    row = session.scalar(select(TgAuthorizationOnlineAbcItem.id).where(
        TgAuthorizationOnlineAbcItem.batch_id == item.batch_id,
        TgAuthorizationOnlineAbcItem.id != item.id,
        TgAuthorizationOnlineAbcItem.outcome == PRIMARY_DRIFT_OUTCOME,
    ).limit(1))
    return bool(row)


def _backup_preview(session, batch, item) -> dict:
    return preview_abc_backup(
        session,
        item.tenant_id,
        item.account_id,
        idempotency_key=online_abc_operation_keys(batch, item)["b"],
    )


def _inputs(session, batch_id: str, account_id: int, case_id: str):
    batch = session.get(TgAuthorizationOnlineAbcBatch, batch_id)
    item = session.scalar(select(TgAuthorizationOnlineAbcItem).where(
        TgAuthorizationOnlineAbcItem.batch_id == batch_id,
        TgAuthorizationOnlineAbcItem.account_id == account_id,
    ))
    case = session.get(TgAuthorizationLocalActivateCase, case_id)
    if not batch or not item or not case:
        raise AuthorizationDrError("online_abc_recovery_unavailable", "ABC recovery inputs are unavailable")
    return batch, item, case


def _audit_recovery(
    session, batch, item, case_id: str, idempotency_key: str, fingerprint: str,
    actor: str, approval_ref: str, action: str,
) -> None:
    audit(
        session,
        tenant_id=batch.tenant_id,
        actor=actor.strip(),
        action=action,
        target_type="tg_authorization_online_abc_items",
        target_id=item.id,
        detail=(
            f"idempotency_key={idempotency_key.strip()}; fingerprint={fingerprint}; "
            f"case_id={case_id}; approval_ref={approval_ref.strip()}"
        ),
    )


def _existing_audit(session, item_id: str, key: str, action: str):
    token = f"idempotency_key={key.strip()};"
    rows = session.scalars(select(AuditLog).where(
        AuditLog.action == action,
        AuditLog.target_type == "tg_authorization_online_abc_items",
        AuditLog.target_id == item_id,
    ).order_by(AuditLog.id.desc()))
    return next((row for row in rows if token in row.detail), None)


def _key(value: str) -> str:
    key = value.strip()
    if not key:
        raise AuthorizationDrError("idempotency_key_required", "ABC recovery key is required")
    return key


def _result(item, case_id: str, fingerprint: str, *, already_applied: bool) -> dict:
    return {
        "batch_id": item.batch_id,
        "item_id": item.id,
        "account_id": item.account_id,
        "case_id": case_id,
        "primary_authorization_id": item.primary_authorization_id,
        "standby_1_plan": item.standby_1_plan,
        "status": item.status,
        "outcome": item.outcome,
        "blocker_code": item.blocker_code,
        "fingerprint": fingerprint,
        "already_applied": already_applied,
    }


__all__ = [
    "COMPLETED_REBASE_BLOCKER",
    "apply_completed_rebase",
    "apply_pre_remote_rearm",
    "preview_completed_rebase",
    "preview_pre_remote_rearm",
    "require_completed_rebase_resume",
]

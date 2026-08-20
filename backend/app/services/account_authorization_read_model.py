from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    TgAccount,
    TgAccountAuthorization,
    TgAuthorizationRestoreProbeFact,
    TgAuthorizationWakeBundle,
    TgAuthorizationWakeBundleCopy,
    TgAuthorizationWakeInventoryEntry,
)

from .account_authorization_constants import (
    ACTIVE_STATUSES,
    EXPLICIT_PRIMARY_SOURCE,
    LEGACY_PRIMARY_SOURCE,
    NO_STANDBY_HINT,
    PRIMARY_ROLE,
    STANDBY_ROLES,
)

AUTHORIZATION_ROLES = (PRIMARY_ROLE, "standby_1", "standby_2")
DOWN_HEALTH_STATUSES = {"expired", "failed", "down", "session_expired", "invalid"}
WAITING_CODE_STATUSES = {"waiting_code", "code_required"}
WAITING_2FA_STATUSES = {"waiting_2fa", "two_fa_required"}
REFRESHING_STATUSES = {"refreshing", "provisioning", "self_healing"}


def authorization_summary_for_account(session: Session, account: TgAccount) -> dict[str, Any]:
    rows = _authorization_rows(session, account)
    if rows:
        return _summary_with_legacy_primary(account, rows)
    return _legacy_summary(account)


def authorization_summaries_for_accounts(session: Session, accounts: list[TgAccount]) -> dict[int, dict[str, Any]]:
    if not accounts:
        return {}
    rows_by_account = _rows_by_account(session, [account.id for account in accounts])
    return {account.id: _summary_for_rows_or_legacy(rows_by_account.get(account.id), account) for account in accounts}


def list_account_authorizations(session: Session, account_id: int) -> list[dict[str, Any]]:
    account = _require_account(session, account_id)
    rows = _authorization_rows(session, account)
    if not rows and account.session_ciphertext:
        return [_legacy_authorization_snapshot(account)]
    return [_authorization_snapshot(session, account, row) for row in rows]


def _authorization_rows(session: Session, account: TgAccount) -> list[TgAccountAuthorization]:
    return list(
        session.scalars(
            select(TgAccountAuthorization)
            .where(TgAccountAuthorization.account_id == account.id, TgAccountAuthorization.disabled_at.is_(None))
            .order_by(
                TgAccountAuthorization.is_current.desc(),
                TgAccountAuthorization.is_slot_current.desc(),
                TgAccountAuthorization.slot_generation.desc(),
                TgAccountAuthorization.id.asc(),
            )
        )
    )


def _rows_by_account(session: Session, account_ids: list[int]) -> dict[int, list[TgAccountAuthorization]]:
    rows = list(
        session.scalars(
            select(TgAccountAuthorization)
            .where(TgAccountAuthorization.account_id.in_(account_ids), TgAccountAuthorization.disabled_at.is_(None))
            .order_by(TgAccountAuthorization.account_id.asc(), TgAccountAuthorization.is_current.desc())
        )
    )
    result: dict[int, list[TgAccountAuthorization]] = {account_id: [] for account_id in account_ids}
    for row in rows:
        result.setdefault(row.account_id, []).append(row)
    return result


def _summary_for_rows_or_legacy(rows: list[TgAccountAuthorization] | None, account: TgAccount) -> dict[str, Any]:
    return _summary_with_legacy_primary(account, rows) if rows else _legacy_summary(account)


def _summary_with_legacy_primary(account: TgAccount, rows: list[TgAccountAuthorization]) -> dict[str, Any]:
    if _has_explicit_primary(rows) or not account.session_ciphertext:
        return _explicit_summary(rows)
    standby_count = sum(1 for row in rows if _is_healthy_standby(row))
    slot_statuses = _slot_statuses(rows)
    slot_statuses[PRIMARY_ROLE] = "healthy"
    return _summary(
        primary_status="active",
        primary_source=LEGACY_PRIMARY_SOURCE,
        standby_count=standby_count,
        is_blocking=False,
        risk_hint="" if standby_count else NO_STANDBY_HINT,
        slot_statuses=slot_statuses,
        can_rescue=_has_switchable_sv_standby(rows),
    )


def _explicit_summary(rows: list[TgAccountAuthorization]) -> dict[str, Any]:
    primary = _primary_row(rows)
    standby_count = sum(1 for row in rows if _is_healthy_standby(row))
    primary_status = primary.status if primary else "missing"
    is_blocking = primary_status not in {"active", "standby"} and standby_count == 0
    slot_statuses = _slot_statuses(rows)
    return _summary(
        primary_status=primary_status,
        primary_source=EXPLICIT_PRIMARY_SOURCE,
        standby_count=standby_count,
        is_blocking=is_blocking,
        risk_hint="" if standby_count else NO_STANDBY_HINT,
        slot_statuses=slot_statuses,
        can_rescue=_has_switchable_sv_standby(rows),
    )


def _legacy_summary(account: TgAccount) -> dict[str, Any]:
    has_session = bool(account.session_ciphertext)
    return _summary(
        primary_status="active" if has_session else "missing",
        primary_source=LEGACY_PRIMARY_SOURCE,
        standby_count=0,
        is_blocking=not has_session,
        risk_hint=NO_STANDBY_HINT if has_session else "账号没有可用主授权 session",
        slot_statuses={
            PRIMARY_ROLE: "healthy" if has_session else "missing",
            "standby_1": "missing",
            "standby_2": "missing",
        },
        can_rescue=False,
    )


def _summary(
    *,
    primary_status: str,
    primary_source: str,
    standby_count: int,
    is_blocking: bool,
    risk_hint: str,
    slot_statuses: dict[str, str],
    can_rescue: bool,
) -> dict[str, Any]:
    healthy_slot_count = sum(1 for status in slot_statuses.values() if status == "healthy")
    return {
        "primary_status": primary_status,
        "primary_source": primary_source,
        "standby_count": standby_count,
        "target_standby_count": 2,
        "has_standby": standby_count > 0,
        "is_blocking": is_blocking,
        "risk_hint": risk_hint,
        "slot_statuses": slot_statuses,
        "aggregate_status": _aggregate_status(slot_statuses),
        "healthy_slot_count": healthy_slot_count,
        "can_rescue": can_rescue and slot_statuses.get(PRIMARY_ROLE) != "healthy",
    }


def _has_switchable_sv_standby(rows: list[TgAccountAuthorization]) -> bool:
    return any(
        _effective_logical_slot(row) == "standby_1"
        and row.provision_region_code == "sv"
        and row.credential_storage_scope == "central_business"
        and bool(row.session_ciphertext)
        and _derive_slot_status(row) == "healthy"
        for row in rows
    )


def _primary_row(rows: list[TgAccountAuthorization]) -> TgAccountAuthorization | None:
    for row in rows:
        if row.is_current or row.role == PRIMARY_ROLE:
            return row
    return rows[0] if rows else None


def _is_healthy_standby(row: TgAccountAuthorization) -> bool:
    return _effective_logical_slot(row) in STANDBY_ROLES and row.is_slot_current and _derive_slot_status(row) == "healthy"


def _has_explicit_primary(rows: list[TgAccountAuthorization]) -> bool:
    return any(row.is_current or row.role == PRIMARY_ROLE for row in rows)


def _require_account(session: Session, account_id: int) -> TgAccount:
    account = session.get(TgAccount, account_id)
    if not account or account.deleted_at is not None:
        raise ValueError("account not found")
    return account


def _legacy_authorization_snapshot(account: TgAccount) -> dict[str, Any]:
    return {
        "id": None,
        "account_id": account.id,
        "role": PRIMARY_ROLE,
        "developer_app_id": account.developer_app_id,
        "developer_app_api_id": account.developer_api_id,
        "proxy_id": account.proxy_id,
        "status": "active",
        "health_status": "legacy",
        "derived_status": "healthy",
        "is_current": True,
        "session_available": True,
        "primary_source": LEGACY_PRIMARY_SOURCE,
        "failure_reason": "",
        "last_health_check_at": None,
        "last_success_at": None,
        "last_switched_at": None,
        "disabled_at": None,
    }


def _authorization_snapshot(session: Session, account: TgAccount, row: TgAccountAuthorization) -> dict[str, Any]:
    derived_status = _derive_slot_status(row)
    return {
        "id": row.id,
        "account_id": row.account_id,
        "role": row.role,
        "logical_slot": _effective_logical_slot(row),
        "slot_generation": row.slot_generation,
        "is_slot_current": row.is_slot_current,
        "provision_region_code": row.provision_region_code,
        "credential_storage_scope": row.credential_storage_scope,
        "developer_app_id": row.developer_app_id,
        "developer_app_api_id": _developer_app_api_id(row),
        "proxy_id": row.proxy_id,
        "status": row.status,
        "health_status": row.health_status,
        "derived_status": derived_status,
        "is_current": row.is_current,
        "session_available": _authorization_session_available(row),
        "primary_source": EXPLICIT_PRIMARY_SOURCE,
        "failure_reason": row.failure_reason,
        "last_health_check_at": row.last_health_check_at,
        "last_success_at": row.last_success_at,
        "last_switched_at": row.last_switched_at,
        "disabled_at": row.disabled_at,
        "dr_state": row.dr_state,
        "remote_authorization_state": row.remote_authorization_state,
        "protected_from_cleanup": row.protected_from_cleanup,
        "telegram_login_at": row.telegram_login_at,
        "migration_recovery_gate_status": row.migration_recovery_gate_status,
        "rollback_window_closed_at": row.rollback_window_closed_at,
        "business_runtime_status": account.business_runtime_status,
        "sv_redundancy_status": account.sv_redundancy_status,
        "authorization_recovery_status": account.authorization_recovery_status,
        **_bundle_snapshot(session, row),
    }


def _slot_statuses(rows: list[TgAccountAuthorization]) -> dict[str, str]:
    by_role = {_effective_logical_slot(row): row for row in rows if row.is_slot_current}
    return {
        role: _derive_slot_status(by_role[role]) if role in by_role else "missing"
        for role in AUTHORIZATION_ROLES
    }


def _effective_logical_slot(row: TgAccountAuthorization) -> str:
    if row.role in STANDBY_ROLES and row.logical_slot == PRIMARY_ROLE:
        return row.role
    return row.logical_slot or row.role


def _derive_slot_status(row: TgAccountAuthorization) -> str:
    if row.disabled_at is not None:
        return "disabled"
    if row.status in REFRESHING_STATUSES:
        return "refreshing"
    if row.status in WAITING_CODE_STATUSES:
        return "waiting_code"
    if row.status in WAITING_2FA_STATUSES:
        return "waiting_2fa"
    if not _authorization_session_available(row):
        return "manual_required"
    health_status = (row.health_status or "").lower()
    if health_status in DOWN_HEALTH_STATUSES or row.status not in ACTIVE_STATUSES:
        return "down"
    return "healthy"


def _aggregate_status(slot_statuses: dict[str, str]) -> str:
    healthy_count = sum(1 for status in slot_statuses.values() if status == "healthy")
    if healthy_count == len(AUTHORIZATION_ROLES):
        return "all_healthy"
    if healthy_count > 0:
        return "recoverable"
    if any(status in {"down", "manual_required"} for status in slot_statuses.values()):
        return "previously_logged_in_all_down"
    return "all_down"


def _developer_app_api_id(row: TgAccountAuthorization) -> int:
    if row.developer_app_api_id_snapshot:
        return int(row.developer_app_api_id_snapshot)
    if row.developer_app:
        return int(row.developer_app.api_id)
    return 0


def _authorization_session_available(row: TgAccountAuthorization) -> bool:
    if row.session_ciphertext:
        return True
    return (
        row.credential_storage_scope == "malaysia_wake_bundle"
        and row.dr_state == "dormant_ready"
        and row.migration_recovery_gate_status == "passed"
        and bool(row.wake_bundle_id)
    )


def _bundle_snapshot(session: Session, row: TgAccountAuthorization) -> dict[str, Any]:
    empty = {
        "wake_bundle_id": None,
        "wake_bundle_generation": 0,
        "recoverable_copy_count": 0,
        "kms_recovery_status": "not_applicable",
        "local_copy_last_verified_at": None,
        "object_copy_last_verified_at": None,
        "last_restore_probe_at": None,
        "my_inventory_sequence": 0,
    }
    if not row.wake_bundle_id:
        return empty
    bundle = session.get(TgAuthorizationWakeBundle, row.wake_bundle_id)
    if not bundle:
        return {**empty, "kms_recovery_status": "bundle_missing"}
    copies = list(session.scalars(select(TgAuthorizationWakeBundleCopy).where(
        TgAuthorizationWakeBundleCopy.bundle_id == bundle.id,
    )))
    by_kind = {copy.copy_kind: copy for copy in copies}
    probe_at = session.scalar(select(TgAuthorizationRestoreProbeFact.observed_at).where(
        TgAuthorizationRestoreProbeFact.bundle_id == bundle.id,
        TgAuthorizationRestoreProbeFact.status == "passed",
    ).order_by(TgAuthorizationRestoreProbeFact.probe_generation.desc()).limit(1))
    inventory_sequence = session.scalar(select(TgAuthorizationWakeInventoryEntry.inventory_sequence).where(
        TgAuthorizationWakeInventoryEntry.bundle_id == bundle.id,
    ).order_by(TgAuthorizationWakeInventoryEntry.inventory_sequence.desc()).limit(1))
    return {
        "wake_bundle_id": bundle.id,
        "wake_bundle_generation": bundle.bundle_generation,
        "recoverable_copy_count": bundle.recoverable_copy_count,
        "kms_recovery_status": bundle.kms_decrypt_status,
        "local_copy_last_verified_at": getattr(by_kind.get("local_persistent"), "readback_verified_at", None),
        "object_copy_last_verified_at": getattr(by_kind.get("object_snapshot"), "readback_verified_at", None),
        "last_restore_probe_at": probe_at,
        "my_inventory_sequence": int(inventory_sequence or 0),
    }

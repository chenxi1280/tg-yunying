from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

from sqlalchemy import select

from app.models import TelegramDeveloperApp, TgAccount, TgAccountAuthorization
from app.security import decrypt_session, encrypt_secret
from app.services._common import _now, audit, gateway
from app.services.developer_apps import credentials_for_account, credentials_for_developer_app

from .contracts import AuthorizationDrError


@dataclass(frozen=True)
class SvRepairCandidate:
    account_id: int
    authorization_id: int
    fact_version: int
    primary_app_id: int
    repair_app_id: int
    standby_2_app_id: int
    conflicting_standby_id: int | None = None
    conflicting_standby_fact_version: int = 0


def preview_sv_redundancy_repair(session, tenant_id: int, account_ids: list[int]) -> dict:
    normalized = sorted(set(account_ids))
    if not normalized:
        raise AuthorizationDrError("empty_target_set", "At least one account is required")
    candidates = [_repair_candidate(session, tenant_id, account_id) for account_id in normalized]
    payload = [asdict(candidate) for candidate in candidates]
    return {
        "tenant_id": tenant_id,
        "target_count": len(payload),
        "target_set_fingerprint": _manifest_fingerprint(tenant_id, payload),
        "targets": payload,
    }


def apply_sv_redundancy_repair(
    session,
    tenant_id: int,
    account_ids: list[int],
    *,
    expected_fingerprint: str,
    actor: str,
    approval_ref: str,
) -> dict:
    if not actor.strip() or not approval_ref.strip():
        raise AuthorizationDrError("approval_ref_required", "Actor and approval reference are required")
    preview = preview_sv_redundancy_repair(session, tenant_id, account_ids)
    if preview["target_set_fingerprint"] != expected_fingerprint:
        raise AuthorizationDrError("migration_fingerprint_conflict", "SV repair target set changed")
    results = [_apply_candidate(session, SvRepairCandidate(**item), actor, approval_ref) for item in preview["targets"]]
    return {
        **preview,
        "succeeded_count": sum(item["status"] == "succeeded" for item in results),
        "failed_count": sum(item["status"] == "failed" for item in results),
        "results": results,
    }


def _apply_candidate(session, frozen: SvRepairCandidate, actor: str, approval_ref: str) -> dict:
    try:
        identity = _probe_candidate(session, frozen)
        row, conflicting = _locked_candidate(session, frozen)
        if conflicting:
            _demote_conflicting_standby(conflicting)
        _promote_repair(row, identity)
        audit(
            session,
            tenant_id=row.tenant_id,
            actor=actor,
            action="恢复硅谷 standby_1 授权",
            target_type="tg_account_authorization",
            target_id=str(row.id),
            detail=f"account_id={row.account_id}; approval_ref={approval_ref}",
        )
        session.commit()
        return {"account_id": frozen.account_id, "authorization_id": frozen.authorization_id, "status": "succeeded"}
    except Exception as exc:  # noqa: BLE001 - every production row failure is returned explicitly.
        session.rollback()
        return {
            "account_id": frozen.account_id,
            "authorization_id": frozen.authorization_id,
            "status": "failed",
            "error": str(exc),
        }


def _probe_candidate(session, frozen: SvRepairCandidate):
    account = session.get(TgAccount, frozen.account_id)
    row = session.get(TgAccountAuthorization, frozen.authorization_id)
    app = session.get(TelegramDeveloperApp, frozen.repair_app_id)
    if not account or not row or not app:
        raise AuthorizationDrError("authorization_version_conflict", "Frozen SV repair input is missing")
    current = gateway.authorization_identity(
        _raw_session(account.session_ciphertext),
        credentials_for_account(session, account),
    )
    repair = gateway.authorization_identity(
        _raw_session(row.session_ciphertext),
        credentials_for_developer_app(app),
    )
    if current.telegram_user_id_digest != repair.telegram_user_id_digest:
        raise AuthorizationDrError("authorization_identity_mismatch", "Repair Session belongs to a different Telegram account")
    if current.auth_key_fingerprint_digest == repair.auth_key_fingerprint_digest:
        raise AuthorizationDrError("authorization_identity_mismatch", "Repair Session duplicates the current SV Session")
    return repair


def _locked_candidate(session, frozen: SvRepairCandidate):
    row = session.scalar(select(TgAccountAuthorization).where(
        TgAccountAuthorization.id == frozen.authorization_id,
    ).with_for_update())
    conflicting = None
    if frozen.conflicting_standby_id:
        conflicting = session.scalar(select(TgAccountAuthorization).where(
            TgAccountAuthorization.id == frozen.conflicting_standby_id,
        ).with_for_update())
    current = _repair_candidate(session, row.tenant_id if row else 0, frozen.account_id)
    if current != frozen:
        raise AuthorizationDrError("authorization_version_conflict", "Frozen SV repair input changed")
    return row, conflicting


def _demote_conflicting_standby(row: TgAccountAuthorization) -> None:
    row.role = "standby_repair"
    row.logical_slot = "standby_repair"
    row.status = "needs_repair"
    row.health_status = "unknown"
    row.derived_status = "needs_repair"
    row.protected_from_cleanup = True
    row.failure_reason = "Retained after duplicate-App standby slot repair"
    row.fact_version += 1


def _promote_repair(row: TgAccountAuthorization, identity) -> None:
    row.role = "standby_1"
    row.logical_slot = "standby_1"
    row.is_slot_current = True
    row.status = "standby"
    row.health_status = "healthy"
    row.derived_status = "healthy"
    row.provision_region_code = "sv"
    row.remote_authorization_state = "active"
    row.telegram_authorization_hash_ciphertext = encrypt_secret(identity.authorization_hash)
    row.auth_key_fingerprint_digest = identity.auth_key_fingerprint_digest
    row.telegram_user_id_digest = identity.telegram_user_id_digest
    row.developer_app_api_id_snapshot = row.developer_app.api_id
    row.protected_from_cleanup = True
    row.failure_reason = ""
    row.last_health_check_at = _now()
    row.last_success_at = _now()
    row.fact_version += 1


def _repair_candidate(session, tenant_id: int, account_id: int) -> SvRepairCandidate:
    account = session.get(TgAccount, account_id)
    if not account or account.tenant_id != tenant_id or not account.session_ciphertext or not account.developer_app_id:
        raise AuthorizationDrError("sv_redundancy_incomplete", f"Account {account_id} current SV Session is unavailable")
    repair = _unique_role(session, account_id, "standby_repair")
    standby_2 = _unique_role(session, account_id, "standby_2")
    conflicting = _conflicting_standby(session, account)
    if not _repair_row_usable(repair) or not _standby_2_row_usable(standby_2):
        raise AuthorizationDrError("sv_redundancy_incomplete", f"Account {account_id} repair inputs are incomplete")
    app_ids = {account.developer_app_id, repair.developer_app_id, standby_2.developer_app_id}
    if None in app_ids or len(app_ids) != 3:
        raise AuthorizationDrError("developer_app_slot_assignment_conflict", "Account repair slots must use three distinct apps")
    return SvRepairCandidate(
        account_id, repair.id, repair.fact_version, account.developer_app_id,
        repair.developer_app_id, standby_2.developer_app_id,
        conflicting.id if conflicting else None,
        conflicting.fact_version if conflicting else 0,
    )


def _unique_role(session, account_id: int, role: str) -> TgAccountAuthorization:
    rows = list(session.scalars(select(TgAccountAuthorization).where(
        TgAccountAuthorization.account_id == account_id,
        TgAccountAuthorization.role == role,
        TgAccountAuthorization.disabled_at.is_(None),
    )))
    if len(rows) != 1:
        raise AuthorizationDrError("sv_redundancy_incomplete", f"Account {account_id} {role} is not unique")
    return rows[0]


def _conflicting_standby(session, account: TgAccount):
    rows = list(session.scalars(select(TgAccountAuthorization).where(
        TgAccountAuthorization.account_id == account.id,
        TgAccountAuthorization.logical_slot == "standby_1",
        TgAccountAuthorization.is_slot_current.is_(True),
        TgAccountAuthorization.disabled_at.is_(None),
    )))
    if not rows:
        return None
    if len(rows) != 1:
        raise AuthorizationDrError("sv_redundancy_incomplete", f"Account {account.id} standby_1 is not unique")
    row = rows[0]
    valid_conflict = (
        row.developer_app_id == account.developer_app_id
        and row.session_ciphertext
        and row.protected_from_cleanup
        and row.status in {"active", "standby"}
        and row.health_status == "healthy"
    )
    if not valid_conflict:
        raise AuthorizationDrError("sv_redundancy_already_ready", f"Account {account.id} already has standby_1")
    return row


def _repair_row_usable(row: TgAccountAuthorization) -> bool:
    return bool(row.session_ciphertext and row.developer_app_id and row.status == "needs_repair")


def _standby_2_row_usable(row: TgAccountAuthorization) -> bool:
    return bool(row.session_ciphertext and row.developer_app_id and row.status in {"active", "standby"})


def _raw_session(ciphertext: str | None) -> str:
    value = decrypt_session(ciphertext)
    if not value:
        raise AuthorizationDrError("sv_redundancy_incomplete", "Session is unavailable")
    return value


def _manifest_fingerprint(tenant_id: int, targets: list[dict]) -> str:
    raw = json.dumps({"tenant_id": tenant_id, "targets": targets}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


__all__ = ["apply_sv_redundancy_repair", "preview_sv_redundancy_repair"]

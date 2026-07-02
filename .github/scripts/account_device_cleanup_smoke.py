from __future__ import annotations

import json
import os
import hashlib
import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from app.database import SessionLocal
from app.models import AccountStatus, TgAccount, TgAccountAuthorization
from app.services.account_authorizations import refresh_authorization_slot, self_heal_authorizations
from app.services.account_security import cleanup_devices_from_precheck, create_device_cleanup_precheck


ACTOR = "github-actions-account-device-cleanup-smoke"
ACTIVE_AUTH_STATUSES = {"active", "standby"}
DEFAULT_MAX_SCAN = 64
STANDBY_ROLES = {"standby_1", "standby_2"}


@dataclass(frozen=True)
class CandidateResult:
    account: TgAccount
    primary: TgAccountAuthorization | None
    standby: TgAccountAuthorization | None
    precheck: dict[str, Any]


def main() -> None:
    tenant_id = _int_env("ACCOUNT_DEVICE_CLEANUP_TENANT_ID", 1)
    account_id = _optional_int_env("ACCOUNT_DEVICE_CLEANUP_ACCOUNT_ID")
    phone_sha256 = os.getenv("ACCOUNT_DEVICE_CLEANUP_PHONE_SHA256", "").strip().lower()
    apply = _bool_env("ACCOUNT_DEVICE_CLEANUP_APPLY")
    max_scan = _int_env("ACCOUNT_DEVICE_CLEANUP_MAX_SCAN", DEFAULT_MAX_SCAN)
    with SessionLocal() as session:
        result = _select_candidate(session, tenant_id, account_id, phone_sha256, max_scan)
        before = _authorization_state(session, result.account.id)
        payload: dict[str, Any] = {
            "mode": "apply" if apply else "dry_run",
            "selected": _account_summary(result.account, result.primary, result.standby),
            "precheck": _compact_precheck(result.precheck),
            "authorization_before": before,
        }
        if apply:
            payload["cleanup_result"] = _cleanup(session, tenant_id, result.account.id, result.precheck["precheck_id"])
            payload.update(_authorization_recovery_smoke(session, result))
            payload["authorization_after"] = _authorization_state(session, result.account.id)
        print("ACCOUNT_DEVICE_CLEANUP_SMOKE=" + json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)


def _select_candidate(
    session,
    tenant_id: int,
    account_id: int | None,
    phone_sha256: str,
    max_scan: int,
) -> CandidateResult:
    accounts = _candidate_accounts(session, tenant_id, account_id, phone_sha256, max_scan)
    if not accounts:
        raise RuntimeError("no active non-code-receiver account with session")
    fallback_any: CandidateResult | None = None
    fallback_cleanup: CandidateResult | None = None
    fallback_standby: CandidateResult | None = None
    errors: list[dict[str, Any]] = []
    for account in accounts:
        primary = _primary_authorization(session, account.id)
        standby = _healthy_standby(session, account.id)
        try:
            precheck = create_device_cleanup_precheck(session, tenant_id, account.id, ACTOR)
        except Exception as exc:  # noqa: BLE001 - production smoke must expose per-account scan failures.
            errors.append({"account_id": account.id, "error": str(exc)})
            continue
        result = CandidateResult(account=account, primary=primary, standby=standby, precheck=precheck)
        cleanup_ready = int(precheck.get("cleanup_count") or 0) > 0
        standby_ready = primary is not None and standby is not None
        if cleanup_ready and standby_ready:
            return result
        fallback_cleanup = fallback_cleanup or result if cleanup_ready else fallback_cleanup
        fallback_standby = fallback_standby or result if standby_ready else fallback_standby
        fallback_any = fallback_any or result
    fallback = fallback_cleanup or fallback_standby or fallback_any
    if fallback:
        return fallback
    raise RuntimeError("candidate scan failed: " + json.dumps(errors, ensure_ascii=False, sort_keys=True))


def _candidate_accounts(
    session,
    tenant_id: int,
    account_id: int | None,
    phone_sha256: str,
    max_scan: int,
) -> list[TgAccount]:
    if account_id is not None and phone_sha256:
        raise RuntimeError("account id and phone hash cannot be used together")
    query = (
        select(TgAccount)
        .where(
            TgAccount.tenant_id == tenant_id,
            TgAccount.deleted_at.is_(None),
            TgAccount.account_identity != "code_receiver",
            TgAccount.status == AccountStatus.ACTIVE.value,
            TgAccount.session_ciphertext.is_not(None),
        )
        .order_by(TgAccount.id.asc())
    )
    if account_id is not None:
        query = query.where(TgAccount.id == account_id)
    elif phone_sha256:
        return _accounts_matching_phone_hash(session, query, phone_sha256)
    else:
        query = query.limit(max(1, max_scan))
    return list(session.scalars(query))


def _accounts_matching_phone_hash(session, query, phone_sha256: str) -> list[TgAccount]:
    matches = [
        account
        for account in session.scalars(query)
        if _phone_digits_sha256(account.phone_number) == phone_sha256
    ]
    if len(matches) > 1:
        raise RuntimeError("phone hash matched multiple active accounts")
    return matches


def _phone_digits_sha256(phone_number: str | None) -> str:
    digits = re.sub(r"\D+", "", phone_number or "")
    return hashlib.sha256(digits.encode("utf-8")).hexdigest() if digits else ""


def _primary_authorization(session, account_id: int) -> TgAccountAuthorization | None:
    return session.scalar(
        select(TgAccountAuthorization)
        .where(
            TgAccountAuthorization.account_id == account_id,
            TgAccountAuthorization.disabled_at.is_(None),
            TgAccountAuthorization.session_ciphertext.is_not(None),
            TgAccountAuthorization.role == "primary",
        )
        .order_by(TgAccountAuthorization.is_current.desc(), TgAccountAuthorization.id.asc())
    )


def _healthy_standby(session, account_id: int) -> TgAccountAuthorization | None:
    return session.scalar(
        select(TgAccountAuthorization)
        .where(
            TgAccountAuthorization.account_id == account_id,
            TgAccountAuthorization.disabled_at.is_(None),
            TgAccountAuthorization.session_ciphertext.is_not(None),
            TgAccountAuthorization.role.in_(STANDBY_ROLES),
            TgAccountAuthorization.status.in_(ACTIVE_AUTH_STATUSES),
        )
        .order_by(TgAccountAuthorization.id.asc())
    )


def _cleanup(session, tenant_id: int, account_id: int, precheck_id: str) -> dict[str, Any]:
    return cleanup_devices_from_precheck(session, tenant_id, account_id, precheck_id, ACTOR)


def _authorization_recovery_smoke(session, result: CandidateResult) -> dict[str, Any]:
    if result.primary is None or result.standby is None:
        return {
            "self_heal_result": {
                "status": "blocked_no_standby",
                "detail": "生产没有显式 primary + 健康 standby 授权资产样本，未执行备用接管",
            },
            "refresh_previous_primary_result": {
                "status": "blocked_no_primary_standby_pair",
                "detail": "缺少可刷新旧主授权的显式授权资产",
            },
        }
    self_heal = self_heal_authorizations(
        session,
        result.account.id,
        actor=ACTOR,
        reason="生产 smoke：模拟主授权掉线，验证健康备用授权接管",
    )
    refresh = refresh_authorization_slot(
        session,
        result.account.id,
        result.primary.id,
        actor=ACTOR,
        reason="生产 smoke：健康槽位刷新原主授权",
    )
    return {"self_heal_result": self_heal, "refresh_previous_primary_result": refresh}


def _authorization_state(session, account_id: int) -> list[dict[str, Any]]:
    rows = session.scalars(
        select(TgAccountAuthorization)
        .where(TgAccountAuthorization.account_id == account_id, TgAccountAuthorization.disabled_at.is_(None))
        .order_by(TgAccountAuthorization.id.asc())
    )
    return [
        {
            "id": row.id,
            "role": row.role,
            "status": row.status,
            "health_status": row.health_status,
            "derived_status": row.derived_status,
            "is_current": row.is_current,
            "developer_app_id": row.developer_app_id,
            "proxy_id": row.proxy_id,
            "has_session": bool(row.session_ciphertext),
            "failure_reason": row.failure_reason,
        }
        for row in rows
    ]


def _compact_precheck(precheck: dict[str, Any]) -> dict[str, Any]:
    return {
        "precheck_id": precheck.get("precheck_id"),
        "account_id": precheck.get("account_id"),
        "cleanup_count": precheck.get("cleanup_count"),
        "kept_count": precheck.get("kept_count"),
        "unknown_count": precheck.get("unknown_count"),
        "status": precheck.get("status"),
        "cleanup_devices": _compact_devices(precheck.get("cleanup_devices") or []),
        "kept_devices": _compact_devices(precheck.get("kept_devices") or []),
        "unknown_devices": _compact_devices(precheck.get("unknown_devices") or []),
    }


def _compact_devices(devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "id": item.get("id"),
            "app_name": item.get("app_name"),
            "device_model": item.get("device_model"),
            "platform": item.get("platform"),
            "remote_api_id": item.get("remote_api_id"),
            "classification": item.get("classification"),
            "matched_roles": item.get("matched_roles") or [],
            "cleanup_eligible": item.get("cleanup_eligible"),
        }
        for item in devices
    ]


def _account_summary(
    account: TgAccount,
    primary: TgAccountAuthorization | None,
    standby: TgAccountAuthorization | None,
) -> dict[str, Any]:
    return {
        "account_id": account.id,
        "display_name": account.display_name,
        "phone_masked": account.phone_masked,
        "status": account.status,
        "developer_app_id": account.developer_app_id,
        "proxy_id": account.proxy_id,
        "primary_authorization_id": primary.id if primary else None,
        "standby_authorization_id": standby.id if standby else None,
        "standby_role": standby.role if standby else "",
        "standby_recovery_ready": bool(primary and standby),
    }


def _bool_env(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    return int(value) if value else default


def _optional_int_env(name: str) -> int | None:
    value = os.getenv(name, "").strip()
    return int(value) if value else None


if __name__ == "__main__":
    main()

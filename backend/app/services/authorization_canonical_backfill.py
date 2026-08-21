from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from sqlalchemy import select

from app.models import TelegramDeveloperApp, TgAccount, TgAccountAuthorization
from app.security import decrypt_session, encrypt_secret
from app.services._common import _now, audit, gateway
from app.services.developer_apps import credentials_for_authorization


ONLINE_STATUS = "在线"


@dataclass(frozen=True)
class BackfillItem:
    account_id: int
    outcome: str
    session_digest: str
    auth_key_digest: str
    current_authorization_id: int | None
    developer_app_id: int | None
    proxy_id: int | None
    authorization_generation: int
    authorization_fact_generation: int
    connection_generation: int


def preview_canonical_authorization_backfill(session, tenant_id: int) -> dict:
    items = _build_items(session, tenant_id, for_update=False)
    return _preview_result(tenant_id, items)


def apply_canonical_authorization_backfill(
    session,
    tenant_id: int,
    *,
    expected_fingerprint: str,
    requested_by: str,
    approved_by: str,
    approval_ref: str,
) -> dict:
    _require_approval(requested_by, approved_by, approval_ref)
    items = _build_items(session, tenant_id, for_update=True)
    preview = _preview_result(tenant_id, items)
    if preview["fingerprint"] != expected_fingerprint:
        raise ValueError("canonical backfill fingerprint changed")
    created_ids = _apply_eligible_items(session, tenant_id, items, approved_by)
    audit(
        session,
        tenant_id=tenant_id,
        actor=approved_by,
        action="回填 canonical current A 授权",
        target_type="tg_accounts",
        target_id=str(tenant_id),
        detail=(
            f"approval_ref={approval_ref}; fingerprint={expected_fingerprint}; "
            f"created={len(created_ids)}; missing_session={preview['counts'].get('missing_session', 0)}"
        ),
    )
    session.commit()
    readback = canonical_authorization_backfill_status(session, tenant_id)
    return {**preview, "mode": "apply", "created_count": len(created_ids), "readback": readback}


def canonical_authorization_backfill_status(session, tenant_id: int) -> dict:
    items = _build_items(session, tenant_id, for_update=False)
    preview = _preview_result(tenant_id, items)
    return {
        "tenant_id": tenant_id,
        "total_count": preview["total_count"],
        "counts": preview["counts"],
        "fingerprint": preview["fingerprint"],
    }


def preview_primary_qualification(session, tenant_id: int, account_id: int) -> dict:
    account, current = _primary_projection(session, tenant_id, account_id, for_update=False)
    payload = _primary_qualification_payload(account, current)
    return {**payload, "fingerprint": _digest(json.dumps(payload, sort_keys=True, separators=(",", ":")))}


def qualify_primary_authorization(
    session,
    tenant_id: int,
    account_id: int,
    *,
    expected_fingerprint: str,
    actor: str,
    approval_ref: str,
) -> dict:
    if not actor.strip() or not approval_ref.strip():
        raise ValueError("actor and approval ref are required")
    preview = preview_primary_qualification(session, tenant_id, account_id)
    if preview["fingerprint"] != expected_fingerprint:
        raise ValueError("primary qualification fingerprint changed")
    current = session.get(TgAccountAuthorization, preview["primary_authorization_id"])
    identity = gateway.authorization_identity(
        decrypt_session(current.session_ciphertext),
        credentials_for_authorization(session, current),
    )
    _validate_primary_identity(current, identity)
    account, locked = _primary_projection(session, tenant_id, account_id, for_update=True)
    locked_payload = _primary_qualification_payload(account, locked)
    locked_fingerprint = _digest(json.dumps(locked_payload, sort_keys=True, separators=(",", ":")))
    if locked_fingerprint != expected_fingerprint:
        session.rollback()
        raise ValueError("primary qualification fingerprint changed after Telegram probe")
    _apply_primary_identity(account, locked, identity)
    audit(
        session,
        tenant_id=tenant_id,
        actor=actor,
        action="验证 canonical A 授权身份",
        target_type="tg_account_authorizations",
        target_id=locked.id,
        detail=f"account_id={account_id}; approval_ref={approval_ref}; authorization_hash_present=true",
    )
    session.commit()
    return {
        "tenant_id": tenant_id,
        "account_id": account_id,
        "primary_authorization_id": locked.id,
        "status": "qualified",
        "authorization_hash_present": True,
        "primary_unchanged": True,
    }


def _build_items(session, tenant_id: int, *, for_update: bool) -> list[BackfillItem]:
    query = select(TgAccount).where(
        TgAccount.tenant_id == tenant_id,
        TgAccount.deleted_at.is_(None),
    ).order_by(TgAccount.id)
    if for_update:
        query = query.with_for_update()
    return [_item_for_account(session, account) for account in session.scalars(query)]


def _item_for_account(session, account: TgAccount) -> BackfillItem:
    current = (
        session.get(TgAccountAuthorization, account.current_authorization_id)
        if account.current_authorization_id
        else None
    )
    outcome = _classify(account, current)
    session_digest = _digest(account.session_ciphertext or "")
    auth_key_digest = _auth_key_digest(account.session_ciphertext) if outcome == "eligible" else ""
    if outcome == "eligible" and not auth_key_digest:
        outcome = "session_unreadable"
    return BackfillItem(
        account_id=account.id,
        outcome=outcome,
        session_digest=session_digest,
        auth_key_digest=auth_key_digest,
        current_authorization_id=account.current_authorization_id,
        developer_app_id=account.developer_app_id,
        proxy_id=account.proxy_id,
        authorization_generation=account.authorization_generation,
        authorization_fact_generation=account.authorization_fact_generation,
        connection_generation=account.connection_generation,
    )


def _classify(account: TgAccount, current: TgAccountAuthorization | None) -> str:
    if current:
        valid = (
            current.account_id == account.id
            and current.is_current
            and current.is_slot_current
            and current.logical_slot == "primary"
            and current.session_ciphertext == account.session_ciphertext
            and current.developer_app_id == account.developer_app_id
        )
        return "already_canonical" if valid else "current_conflict"
    if account.current_authorization_id is not None:
        return "current_missing"
    if not account.session_ciphertext:
        return "missing_session"
    if account.developer_app_id is None:
        return "missing_developer_app"
    return "eligible"


def _auth_key_digest(session_ciphertext: str | None) -> str:
    try:
        from telethon.sessions import StringSession

        raw_session = decrypt_session(session_ciphertext)
        auth_key = StringSession(raw_session or "").auth_key
        return hashlib.sha256(auth_key.key).hexdigest() if auth_key else ""
    except (TypeError, ValueError):
        return ""


def _preview_result(tenant_id: int, items: list[BackfillItem]) -> dict:
    counts: dict[str, int] = {}
    for item in items:
        counts[item.outcome] = counts.get(item.outcome, 0) + 1
    body = {
        "tenant_id": tenant_id,
        "items": [item.__dict__ for item in items],
    }
    return {
        "mode": "preview",
        "tenant_id": tenant_id,
        "total_count": len(items),
        "counts": counts,
        "fingerprint": _digest(json.dumps(body, sort_keys=True, separators=(",", ":"))),
    }


def _apply_eligible_items(session, tenant_id: int, items: list[BackfillItem], actor: str) -> list[int]:
    created_ids: list[int] = []
    for item in items:
        if item.outcome != "eligible":
            continue
        account = session.get(TgAccount, item.account_id)
        app = session.get(TelegramDeveloperApp, item.developer_app_id)
        row = _new_primary_authorization(tenant_id, account, app, item, actor)
        session.add(row)
        session.flush()
        account.current_authorization_id = row.id
        created_ids.append(row.id)
    return created_ids


def _new_primary_authorization(tenant_id, account, app, item, actor) -> TgAccountAuthorization:
    is_online = account.status == ONLINE_STATUS
    return TgAccountAuthorization(
        tenant_id=tenant_id,
        account_id=account.id,
        role="primary",
        logical_slot="primary",
        slot_generation=1,
        is_slot_current=True,
        provision_region_code="sv",
        credential_storage_scope="central_business",
        developer_app_id=account.developer_app_id,
        developer_app_api_id_snapshot=app.api_id,
        proxy_id=account.proxy_id,
        session_ciphertext=account.session_ciphertext,
        status="active" if is_online else "needs_repair",
        health_status="healthy" if is_online else "unknown",
        derived_status="active" if is_online else "manual_required",
        is_current=True,
        dr_state="not_configured",
        remote_authorization_state="unknown",
        protected_from_cleanup=True,
        auth_key_fingerprint_digest=item.auth_key_digest,
        created_by=actor,
    )


def _primary_projection(session, tenant_id: int, account_id: int, *, for_update: bool):
    query = select(TgAccount).where(
        TgAccount.id == account_id,
        TgAccount.tenant_id == tenant_id,
        TgAccount.deleted_at.is_(None),
    )
    if for_update:
        query = query.with_for_update()
    account = session.scalar(query)
    current = session.get(TgAccountAuthorization, account.current_authorization_id) if account else None
    valid = (
        account
        and current
        and current.account_id == account.id
        and current.is_current
        and current.is_slot_current
        and current.logical_slot == "primary"
        and current.provision_region_code == "sv"
        and current.session_ciphertext == account.session_ciphertext
        and current.developer_app_id == account.developer_app_id
    )
    if not valid:
        raise ValueError("canonical primary projection is unavailable")
    return account, current


def _primary_qualification_payload(account, current) -> dict:
    app_version = current.developer_app.credentials_version if current.developer_app else 0
    return {
        "tenant_id": account.tenant_id,
        "account_id": account.id,
        "primary_authorization_id": current.id,
        "primary_fact_version": current.fact_version,
        "authorization_generation": account.authorization_generation,
        "authorization_fact_generation": account.authorization_fact_generation,
        "connection_generation": account.connection_generation,
        "developer_app_id": current.developer_app_id,
        "developer_app_credentials_version": app_version,
        "proxy_id": current.proxy_id,
        "session_digest": _digest(current.session_ciphertext or ""),
        "auth_key_digest": current.auth_key_fingerprint_digest,
        "user_id_digest": current.telegram_user_id_digest,
    }


def _validate_primary_identity(current, identity) -> None:
    if not identity.authorization_hash or identity.authorization_hash == "0":
        raise ValueError("primary Telegram authorization hash is unavailable")
    if current.auth_key_fingerprint_digest and (
        identity.auth_key_fingerprint_digest != current.auth_key_fingerprint_digest
    ):
        raise ValueError("primary AuthKey changed during qualification")


def _apply_primary_identity(account, current, identity) -> None:
    current.telegram_user_id_digest = identity.telegram_user_id_digest
    current.auth_key_fingerprint_digest = identity.auth_key_fingerprint_digest
    current.telegram_authorization_hash_ciphertext = encrypt_secret(identity.authorization_hash)
    current.status = "active"
    current.health_status = "healthy"
    current.derived_status = "active"
    current.remote_authorization_state = "active"
    current.last_health_check_at = _now()
    current.last_success_at = _now()
    current.fact_version += 1
    account.authorization_fact_generation += 1


def _require_approval(requested_by: str, approved_by: str, approval_ref: str) -> None:
    if not requested_by.strip() or not approved_by.strip() or not approval_ref.strip():
        raise ValueError("requester, approver and approval ref are required")
    if requested_by.strip() == approved_by.strip():
        raise ValueError("approver must differ from requester")


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


__all__ = [
    "apply_canonical_authorization_backfill",
    "canonical_authorization_backfill_status",
    "preview_canonical_authorization_backfill",
    "preview_primary_qualification",
    "qualify_primary_authorization",
]

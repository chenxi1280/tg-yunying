from __future__ import annotations

import hashlib
import json
from binascii import Error as BinasciiError
from dataclasses import dataclass

from cryptography.fernet import InvalidToken
from sqlalchemy import select

from app.models import TelegramDeveloperApp, TgAccount, TgAccountAuthorization
from app.security import decrypt_session, encrypt_secret
from app.services._common import _now, audit, gateway
from app.services.account_authorization_metadata import resolve_authorization_identity_hash
from app.services.developer_apps import credentials_for_authorization


ONLINE_STATUS = "在线"


@dataclass(frozen=True)
class BackfillItem:
    account_id: int
    outcome: str
    session_digest: str
    auth_key_digest: str
    current_authorization_id: int | None
    existing_current_authorization_id: int | None
    existing_current_fact_version: int
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
    applied = _apply_items(session, tenant_id, items=items, actor=approved_by)
    audit(
        session,
        tenant_id=tenant_id,
        actor=approved_by,
        action="回填 canonical current A 授权",
        target_type="tg_accounts",
        target_id=str(tenant_id),
        detail=(
            f"approval_ref={approval_ref}; fingerprint={expected_fingerprint}; "
            f"created={applied['created_count']}; linked={applied['linked_count']}; "
            f"missing_session={preview['counts'].get('missing_session', 0)}"
        ),
    )
    session.commit()
    readback = canonical_authorization_backfill_status(session, tenant_id)
    return {**preview, "mode": "apply", **applied, "readback": readback}


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
    identity, hash_source = resolve_authorization_identity_hash(
        session,
        account_id,
        identity,
        exclude_authorization_id=current.id,
    )
    _validate_primary_identity(current, identity)
    session.expire_all()
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
        detail=(
            f"account_id={account_id}; approval_ref={approval_ref}; "
            f"authorization_hash_present=true; hash_source={hash_source}"
        ),
    )
    session.commit()
    return {
        "tenant_id": tenant_id,
        "account_id": account_id,
        "primary_authorization_id": locked.id,
        "status": "qualified",
        "authorization_hash_present": True,
        "authorization_hash_source": hash_source,
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
    existing_current = session.scalar(select(TgAccountAuthorization).where(
        TgAccountAuthorization.account_id == account.id,
        TgAccountAuthorization.is_current.is_(True),
    ))
    outcome = _classify(account, current, existing_current)
    session_digest = _digest(account.session_ciphertext or "")
    needs_session_parse = outcome in {"eligible", "link_existing"}
    auth_key_digest = _auth_key_digest(account.session_ciphertext) if needs_session_parse else ""
    if needs_session_parse and not auth_key_digest:
        outcome = "session_unreadable"
    return BackfillItem(
        account_id=account.id,
        outcome=outcome,
        session_digest=session_digest,
        auth_key_digest=auth_key_digest,
        current_authorization_id=account.current_authorization_id,
        existing_current_authorization_id=existing_current.id if existing_current else None,
        existing_current_fact_version=existing_current.fact_version if existing_current else 0,
        developer_app_id=account.developer_app_id,
        proxy_id=account.proxy_id,
        authorization_generation=account.authorization_generation,
        authorization_fact_generation=account.authorization_fact_generation,
        connection_generation=account.connection_generation,
    )


def _classify(account: TgAccount, current, existing_current) -> str:
    if current:
        valid = (
            current.account_id == account.id
            and current.is_current
            and current.is_slot_current
            and current.logical_slot == "primary"
            and current.session_ciphertext == account.session_ciphertext
            and current.developer_app_id == account.developer_app_id
            and existing_current
            and existing_current.id == current.id
        )
        return "already_canonical" if valid else "current_conflict"
    if account.current_authorization_id is not None:
        return "current_missing"
    if existing_current:
        valid = (
            existing_current.logical_slot == "primary"
            and existing_current.is_slot_current
            and existing_current.provision_region_code == "sv"
            and existing_current.session_ciphertext == account.session_ciphertext
            and existing_current.developer_app_id == account.developer_app_id
        )
        return "link_existing" if valid else "current_conflict"
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
    except (BinasciiError, InvalidToken, TypeError, UnicodeDecodeError, ValueError):
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


def _apply_items(session, tenant_id: int, *, items: list[BackfillItem], actor: str) -> dict:
    created_count = 0
    linked_count = 0
    for item in items:
        if item.outcome == "link_existing":
            account = session.get(TgAccount, item.account_id)
            account.current_authorization_id = item.existing_current_authorization_id
            linked_count += 1
            continue
        if item.outcome != "eligible":
            continue
        account = session.get(TgAccount, item.account_id)
        app = session.get(TelegramDeveloperApp, item.developer_app_id)
        row = _new_primary_authorization(tenant_id, account, app=app, item=item, actor=actor)
        session.add(row)
        session.flush()
        account.current_authorization_id = row.id
        created_count += 1
    return {"created_count": created_count, "linked_count": linked_count}


def _new_primary_authorization(tenant_id, account, *, app, item, actor) -> TgAccountAuthorization:
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
        "observer_snapshot": _observer_snapshot(account, current),
    }


def _observer_snapshot(account, current) -> list[list]:
    rows = [
        row for row in account.authorizations
        if row.id != current.id
        and row.disabled_at is None
        and row.status in {"active", "standby"}
        and row.health_status == "healthy"
        and row.is_slot_current
        and row.session_ciphertext
    ]
    return [
        [row.id, row.fact_version, row.logical_slot, row.developer_app_id, _digest(row.session_ciphertext)]
        for row in sorted(rows, key=lambda value: value.id)
    ]


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

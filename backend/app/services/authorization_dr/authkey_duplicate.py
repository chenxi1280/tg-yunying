from __future__ import annotations

import hashlib
import json

from sqlalchemy import select

from app.models import AccountStatus, TgAccount, TgAccountAuthorization, TgAccountOnlineState
from app.services._common import audit

from .contracts import AuthorizationDrError


AUTHKEY_DUPLICATE_CODE = "authorization_key_duplicated"


def preview_authkey_duplicate_projection(session, tenant_id: int, account_ids: list[int]) -> dict:
    targets = [_target(session, tenant_id, account_id) for account_id in _normalized_ids(account_ids)]
    payload = {
        "tenant_id": tenant_id,
        "targets": [_target_payload(account, authorization, state) for account, authorization, state in targets],
    }
    return {**payload, "fingerprint": _fingerprint(payload)}


def apply_authkey_duplicate_projection(
    session,
    tenant_id: int,
    account_ids: list[int],
    *,
    expected_fingerprint: str,
    actor: str,
    approval_ref: str,
) -> dict:
    if not actor.strip() or not approval_ref.strip():
        raise AuthorizationDrError("approval_ref_required", "Projection approval is required")
    preview = preview_authkey_duplicate_projection(session, tenant_id, account_ids)
    if preview["fingerprint"] != expected_fingerprint:
        raise AuthorizationDrError("migration_fingerprint_conflict", "AuthKey duplicate facts changed")
    projected = []
    for account, authorization, state in (
        _target(session, tenant_id, account_id) for account_id in _normalized_ids(account_ids)
    ):
        if authorization.last_authoritative_error_code != AUTHKEY_DUPLICATE_CODE:
            _project(account, authorization, state)
        projected.append(account.id)
    audit(
        session,
        tenant_id=tenant_id,
        actor=actor,
        action="回填 current authorization AuthKey duplicate 权威事实",
        target_type="tg_account_authorizations",
        target_id=",".join(str(value) for value in projected),
        detail=f"approval_ref={approval_ref}",
    )
    session.commit()
    return {"tenant_id": tenant_id, "account_ids": projected, "status": "projected"}


def _target(session, tenant_id: int, account_id: int):
    account = session.get(TgAccount, account_id)
    authorization = session.get(TgAccountAuthorization, account.current_authorization_id) if account else None
    state = session.scalar(select(TgAccountOnlineState).where(
        TgAccountOnlineState.tenant_id == tenant_id,
        TgAccountOnlineState.account_id == account_id,
    ))
    valid = (
        account
        and authorization
        and state
        and account.tenant_id == tenant_id
        and account.status == AccountStatus.SESSION_EXPIRED.value
        and authorization.account_id == account.id
        and authorization.is_current
        and state.failure_detail.startswith("AuthKeyDuplicatedError:")
        and state.last_probe_at
    )
    if not valid:
        raise AuthorizationDrError("authkey_duplicate_unproven", f"Account {account_id} lacks typed evidence")
    return account, authorization, state


def _target_payload(account, authorization, state) -> dict:
    return {
        "account_id": account.id,
        "current_authorization_id": authorization.id,
        "authorization_fact_generation": account.authorization_fact_generation,
        "authorization_fact_version": authorization.fact_version,
        "last_probe_at": str(state.last_probe_at),
        "failure_digest": _fingerprint({"detail": state.failure_detail}),
    }


def _project(account, authorization, state) -> None:
    authorization.health_status = "invalid"
    authorization.dr_state = "invalid"
    authorization.failure_reason = state.failure_detail[:500]
    authorization.last_authoritative_error_code = AUTHKEY_DUPLICATE_CODE
    authorization.last_authoritative_observed_at = state.last_probe_at
    authorization.fact_version += 1
    account.authorization_fact_generation += 1


def _normalized_ids(account_ids: list[int]) -> list[int]:
    values = sorted({int(value) for value in account_ids})
    if not values:
        raise AuthorizationDrError("account_not_found", "At least one account is required")
    return values


def _fingerprint(payload: dict) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


__all__ = ["apply_authkey_duplicate_projection", "preview_authkey_duplicate_projection"]

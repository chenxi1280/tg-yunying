from __future__ import annotations

import hashlib

from sqlalchemy import select

from app.models import AccountStatus, TgAccount, TgAccountAuthorization, TgAuthorizationOnlineAbcItem
from app.services._common import _now, audit


PRIMARY_DRIFT_OUTCOME = "primary_drift_after_success"
ACKNOWLEDGED_PRIMARY_FAILURE = "primary_and_sv_standby_unavailable"


def primary_state(account, primary, item) -> str:
    if not _structural_primary(account, primary):
        return "drifted"
    frozen = _primary_dimensions(account, primary, item, fact_offset=0)
    if frozen and _healthy_primary(primary):
        return "frozen"
    if frozen and _legacy_primary(primary):
        return "legacy_frozen"
    qualified = _primary_dimensions(account, primary, item, fact_offset=1)
    if qualified and _qualified_primary(primary):
        return "qualified"
    return "drifted"


def stop_completed_primary_drift(session, batch, *, actor: str, approval_ref: str) -> bool:
    if _stop_manual_primary_drift(session, batch, actor=actor, approval_ref=approval_ref):
        return True
    items = list(session.scalars(select(TgAuthorizationOnlineAbcItem).where(
        TgAuthorizationOnlineAbcItem.batch_id == batch.id,
        TgAuthorizationOnlineAbcItem.status == "succeeded",
    )))
    drifted = [item for item in items if _primary_drifted(session, item)]
    if not drifted:
        return False
    for item in drifted:
        _stop_item(item)
    batch.status = "stopped"
    batch.version += 1
    accounts = ",".join(str(item.account_id) for item in drifted)
    audit(
        session,
        tenant_id=batch.tenant_id,
        actor=actor,
        action=f"停止 ABC canary A 漂移 accounts={accounts}",
        target_type="tg_authorization_online_abc_batches",
        target_id=batch.id,
        detail=f"approval_ref={approval_ref}; target_count={batch.target_count}",
    )
    session.commit()
    return True


def _stop_manual_primary_drift(session, batch, *, actor: str, approval_ref: str) -> bool:
    items = list(session.scalars(select(TgAuthorizationOnlineAbcItem).where(
        TgAuthorizationOnlineAbcItem.batch_id == batch.id,
        TgAuthorizationOnlineAbcItem.status == "manual_required",
    )))
    drifted = [item for item in items if _manual_primary_drifted(session, item)]
    if not drifted:
        return False
    batch.status = "stopped"
    batch.version += 1
    accounts = ",".join(str(item.account_id) for item in drifted)
    audit(
        session,
        tenant_id=batch.tenant_id,
        actor=actor,
        action=f"停止 ABC full manual A 漂移 accounts={accounts}",
        target_type="tg_authorization_online_abc_batches",
        target_id=batch.id,
        detail=f"approval_ref={approval_ref}; target_count={batch.target_count}",
    )
    session.commit()
    return True


def _manual_primary_drifted(session, item) -> bool:
    account = session.get(TgAccount, item.account_id)
    primary = session.get(TgAccountAuthorization, item.primary_authorization_id)
    if item.blocker_code == ACKNOWLEDGED_PRIMARY_FAILURE:
        return not _acknowledged_primary_failure_stable(account, primary, item)
    return primary_state(account, primary, item) not in {"frozen", "legacy_frozen", "qualified"}


def _acknowledged_primary_failure_stable(account, primary, item) -> bool:
    fact_delta = account.authorization_fact_generation - item.authorization_fact_generation if account else -1
    primary_delta = primary.fact_version - item.primary_fact_version if primary else -1
    return bool(
        account
        and primary
        and account.status in {AccountStatus.SESSION_EXPIRED.value, AccountStatus.NEED_RELOGIN.value}
        and account.current_authorization_id == primary.id
        and account.session_ciphertext == primary.session_ciphertext
        and _digest(primary.session_ciphertext or "") == item.primary_session_digest
        and account.authorization_generation == item.authorization_generation
        and account.connection_generation == item.connection_generation
        and fact_delta == primary_delta
        and fact_delta >= 1
        and primary.is_current
        and primary.is_slot_current
        and primary.protected_from_cleanup
    )


def _primary_drifted(session, item) -> bool:
    account = session.get(TgAccount, item.account_id)
    primary = session.get(TgAccountAuthorization, item.primary_authorization_id)
    account_delta = account.authorization_fact_generation - item.authorization_fact_generation if account else -1
    primary_delta = primary.fact_version - item.primary_fact_version if primary else -1
    return not (
        account
        and primary
        and account.status == AccountStatus.ACTIVE.value
        and account.current_authorization_id == primary.id
        and primary.is_current
        and primary.health_status == "healthy"
        and _digest(primary.session_ciphertext or "") == item.primary_session_digest
        and account.authorization_generation == item.authorization_generation
        and account_delta == primary_delta and account_delta >= 1
        and account.connection_generation == item.connection_generation
        and not primary.last_authoritative_error_code
    )


def _stop_item(item) -> None:
    item.status = "stopped"
    item.outcome = PRIMARY_DRIFT_OUTCOME
    item.blocker_code = PRIMARY_DRIFT_OUTCOME
    item.finished_at = _now()
    item.version += 1


def _primary_dimensions(account, primary, item, *, fact_offset: int) -> bool:
    return bool(
        account.authorization_generation == item.authorization_generation
        and account.connection_generation == item.connection_generation
        and _fact_dimensions(account, primary, item, fact_offset)
        and _digest(primary.session_ciphertext or "") == item.primary_session_digest
        and primary.is_current
    )


def _structural_primary(account, primary) -> bool:
    return bool(
        account
        and primary
        and account.status == AccountStatus.ACTIVE.value
        and account.current_authorization_id == primary.id
        and account.session_ciphertext == primary.session_ciphertext
        and account.developer_app_id == primary.developer_app_id
        and primary.session_ciphertext
        and primary.is_current
        and primary.is_slot_current
        and primary.logical_slot in {"primary", "standby_1"}
        and primary.provision_region_code == "sv"
        and primary.protected_from_cleanup
    )


def _fact_dimensions(account, primary, item, fact_offset: int) -> bool:
    account_delta = account.authorization_fact_generation - item.authorization_fact_generation
    primary_delta = primary.fact_version - item.primary_fact_version
    if fact_offset == 0:
        return account_delta == primary_delta == 0
    return account_delta == primary_delta and account_delta >= 1


def _healthy_primary(primary) -> bool:
    return bool(
        primary.status == "active" and primary.health_status == "healthy"
        and primary.last_authoritative_error_code == ""
        and primary.disabled_at is None
    )


def _legacy_primary(primary) -> bool:
    return bool(
        primary.status == "active" and primary.health_status == "legacy"
        and primary.last_authoritative_error_code == ""
        and primary.disabled_at is None
    )


def _qualified_primary(primary) -> bool:
    return bool(
        _healthy_primary(primary)
        and primary.telegram_user_id_digest
        and primary.auth_key_fingerprint_digest
    )


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


__all__ = ["primary_state", "stop_completed_primary_drift"]

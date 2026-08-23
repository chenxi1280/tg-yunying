from __future__ import annotations

import hashlib

from sqlalchemy import select

from app.models import AccountStatus, TgAccount, TgAccountAuthorization, TgAuthorizationOnlineAbcItem
from app.services._common import _now, audit


PRIMARY_DRIFT_OUTCOME = "primary_drift_after_success"


def stop_completed_primary_drift(session, batch, *, actor: str, approval_ref: str) -> bool:
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


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


__all__ = ["stop_completed_primary_drift"]

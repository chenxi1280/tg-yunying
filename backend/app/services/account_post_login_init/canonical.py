from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import TelegramDeveloperApp, TgAccount, TgAccountAuthorization
from app.services._common import _now, audit


def ensure_canonical_primary(
    session: Session,
    account: TgAccount,
    actor: str,
) -> TgAccountAuthorization:
    current = _current_primary(session, account)
    if _matches_account(current, account):
        return current
    _retire_old_current_rows(session, account)
    row = _new_primary(session, account, actor)
    session.add(row)
    session.flush()
    account.current_authorization_id = row.id
    if current is not None:
        account.authorization_generation += 1
        account.authorization_fact_generation += 1
        account.connection_generation += 1
    audit(
        session,
        tenant_id=account.tenant_id,
        actor=actor,
        action="批量登录建立 canonical A 授权",
        target_type="tg_account_authorization",
        target_id=str(row.id),
        detail=f"account_id={account.id}; authorization_generation={account.authorization_generation}",
    )
    return row


def _current_primary(session: Session, account: TgAccount) -> TgAccountAuthorization | None:
    if account.current_authorization_id:
        return session.get(TgAccountAuthorization, account.current_authorization_id)
    return session.scalar(
        select(TgAccountAuthorization).where(
            TgAccountAuthorization.account_id == account.id,
            TgAccountAuthorization.is_current.is_(True),
        )
    )


def _matches_account(row: TgAccountAuthorization | None, account: TgAccount) -> bool:
    return bool(
        row
        and row.account_id == account.id
        and row.is_current
        and row.is_slot_current
        and row.logical_slot in {"primary", "standby_1"}
        and row.provision_region_code == "sv"
        and row.session_ciphertext == account.session_ciphertext
        and row.developer_app_id == account.developer_app_id
        and row.disabled_at is None
    )


def _retire_old_current_rows(session: Session, account: TgAccount) -> None:
    rows = session.scalars(
        select(TgAccountAuthorization).where(
            TgAccountAuthorization.account_id == account.id,
            TgAccountAuthorization.is_current.is_(True),
        )
    )
    for row in rows:
        row.is_current = False
        row.is_slot_current = False
        row.role = "authorization_repair"
        row.logical_slot = "authorization_repair"
        row.status = "needs_repair"
        row.health_status = "unknown"
        row.failure_reason = "批量重新登录产生新的 canonical A，保留旧授权待审计"
        row.fact_version += 1


def _new_primary(
    session: Session,
    account: TgAccount,
    actor: str,
) -> TgAccountAuthorization:
    app = session.get(TelegramDeveloperApp, account.developer_app_id)
    if not app or not account.session_ciphertext:
        raise ValueError("canonical A requires developer app and authorized session")
    previous_generation = session.scalar(
        select(func.max(TgAccountAuthorization.slot_generation)).where(
            TgAccountAuthorization.account_id == account.id,
        )
    )
    return TgAccountAuthorization(
        tenant_id=account.tenant_id,
        account_id=account.id,
        role="primary",
        logical_slot="primary",
        slot_generation=(previous_generation or 0) + 1,
        is_slot_current=True,
        provision_region_code="sv",
        credential_storage_scope="central_business",
        developer_app_id=account.developer_app_id,
        developer_app_api_id_snapshot=app.api_id,
        proxy_id=account.proxy_id,
        session_ciphertext=account.session_ciphertext,
        status="active",
        health_status="healthy",
        derived_status="active",
        is_current=True,
        protected_from_cleanup=True,
        telegram_login_at=_now(),
        last_success_at=_now(),
        created_by=actor,
    )


__all__ = ["ensure_canonical_primary"]

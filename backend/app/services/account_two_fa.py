from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import TgAccount, TgAccountSecuritySnapshot
from app.security import decrypt_secret, encrypt_secret

from ._common import _now

MANAGED_TWO_FA_HINT = "TG运营平台托管"


def managed_two_fa_password(session: Session, account: TgAccount) -> str | None:
    snapshot = session.scalar(select(TgAccountSecuritySnapshot).where(TgAccountSecuritySnapshot.account_id == account.id))
    if not snapshot or not snapshot.two_fa_password_ciphertext:
        return None
    return decrypt_secret(snapshot.two_fa_password_ciphertext)


def record_managed_two_fa_password(session: Session, account: TgAccount, password: str, *, last_error: str = "") -> TgAccountSecuritySnapshot:
    snapshot = _snapshot(session, account)
    snapshot.two_fa_status = "enabled"
    snapshot.two_fa_password_ciphertext = encrypt_secret(password)
    snapshot.two_fa_password_hint = MANAGED_TWO_FA_HINT
    snapshot.two_fa_password_stored_at = _now()
    snapshot.last_error = last_error
    return snapshot


def rotate_managed_two_fa_after_login(
    session: Session,
    account: TgAccount,
    *,
    session_ciphertext: str,
    current_password: str,
    credentials: object,
    telegram_gateway: object,
    marker: str,
) -> str:
    record_managed_two_fa_password(session, account, current_password)
    return current_password


def _snapshot(session: Session, account: TgAccount) -> TgAccountSecuritySnapshot:
    snapshot = session.scalar(select(TgAccountSecuritySnapshot).where(TgAccountSecuritySnapshot.account_id == account.id))
    if snapshot:
        return snapshot
    snapshot = TgAccountSecuritySnapshot(tenant_id=account.tenant_id, account_id=account.id)
    session.add(snapshot)
    session.flush()
    return snapshot

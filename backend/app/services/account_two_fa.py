from __future__ import annotations

import secrets

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


def generate_managed_two_fa_password(account: TgAccount, marker: str) -> str:
    token = secrets.token_urlsafe(18)
    return f"TgOps-{account.id}-{marker}-{token}"


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
    new_password = generate_managed_two_fa_password(account, marker)
    result = telegram_gateway.set_two_fa_password(
        session_ciphertext,
        new_password,
        credentials=credentials,
        hint=MANAGED_TWO_FA_HINT,
        current_password=current_password,
    )
    if not result.ok:
        _mark_rotation_failed(session, account, result)
        raise ValueError(f"2FA 登录成功，但修改为平台托管新密码失败：{result.detail or result.failure_type}")
    record_managed_two_fa_password(session, account, new_password)
    return new_password


def _snapshot(session: Session, account: TgAccount) -> TgAccountSecuritySnapshot:
    snapshot = session.scalar(select(TgAccountSecuritySnapshot).where(TgAccountSecuritySnapshot.account_id == account.id))
    if snapshot:
        return snapshot
    snapshot = TgAccountSecuritySnapshot(tenant_id=account.tenant_id, account_id=account.id)
    session.add(snapshot)
    session.flush()
    return snapshot


def _mark_rotation_failed(session: Session, account: TgAccount, result: object) -> None:
    snapshot = _snapshot(session, account)
    snapshot.two_fa_status = "rotation_failed"
    snapshot.last_error = str(getattr(result, "detail", "") or getattr(result, "failure_type", ""))

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import TgAccountFullInitialization, TgAccountSecuritySnapshot


def abc_login_credential_ready(
    session: Session,
    owner: TgAccountFullInitialization,
) -> bool:
    if owner.two_fa_status == "succeeded" and owner.two_fa_evidence_ref:
        return True
    if owner.source_two_fa_kind == "telegram_missing":
        return True
    snapshot = session.scalar(
        select(TgAccountSecuritySnapshot).where(
            TgAccountSecuritySnapshot.account_id == owner.account_id,
        )
    )
    if not snapshot or not snapshot.two_fa_password_ciphertext:
        return False
    if snapshot.two_fa_authorization_generation != owner.authorization_generation:
        return False
    if snapshot.two_fa_password_source == "telegram_accepted_import":
        return True
    return bool(
        snapshot.two_fa_password_source == "platform_fixed_confirmed"
        and snapshot.fixed_two_fa_version == owner.fixed_two_fa_version
        and snapshot.two_fa_evidence_ref
    )


__all__ = ["abc_login_credential_ready"]

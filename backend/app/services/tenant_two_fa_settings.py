from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import Tenant
from app.schemas import TenantFixedTwoFaSettingsOut
from app.security import decrypt_secret, encrypt_secret

from ._common import _now, audit

FIXED_TWO_FA_NOT_CONFIGURED = "固定 2FA 密码未配置，请先在系统设置中配置"
FIXED_TWO_FA_ALREADY_CONFIGURED = "固定 2FA 密码已经设置，不能修改"


def get_tenant_fixed_two_fa_settings(session: Session, *, tenant_id: int) -> TenantFixedTwoFaSettingsOut:
    tenant = _require_tenant(session, tenant_id)
    return _fixed_two_fa_out(tenant)


def set_tenant_fixed_two_fa_password(
    session: Session,
    *,
    tenant_id: int,
    password: str,
    reason: str,
    actor: str,
) -> TenantFixedTwoFaSettingsOut:
    tenant = _require_tenant(session, tenant_id)
    if tenant.fixed_two_fa_password_ciphertext:
        raise ValueError(FIXED_TWO_FA_ALREADY_CONFIGURED)
    fixed_password = password.strip()
    change_reason = reason.strip()
    if not fixed_password:
        raise ValueError("固定 2FA 密码不能为空")
    if not change_reason:
        raise ValueError("操作原因不能为空")
    tenant.fixed_two_fa_password_ciphertext = encrypt_secret(fixed_password)
    tenant.fixed_two_fa_password_set_at = _now()
    tenant.fixed_two_fa_password_set_by = actor
    audit(
        session,
        tenant_id=tenant.id,
        actor=actor,
        action="设置租户固定二步密码",
        target_type="tenant",
        target_id=str(tenant.id),
        detail=change_reason,
    )
    session.commit()
    session.refresh(tenant)
    return _fixed_two_fa_out(tenant)


def tenant_fixed_two_fa_password(session: Session, *, tenant_id: int) -> str | None:
    tenant = _require_tenant(session, tenant_id)
    if not tenant.fixed_two_fa_password_ciphertext:
        return None
    return decrypt_secret(tenant.fixed_two_fa_password_ciphertext)


def _fixed_two_fa_out(tenant: Tenant) -> TenantFixedTwoFaSettingsOut:
    return TenantFixedTwoFaSettingsOut(
        tenant_id=tenant.id,
        fixed_two_fa_password_configured=bool(tenant.fixed_two_fa_password_ciphertext),
        fixed_two_fa_password_set_at=tenant.fixed_two_fa_password_set_at,
    )


def _require_tenant(session: Session, tenant_id: int) -> Tenant:
    tenant = session.get(Tenant, tenant_id)
    if not tenant:
        raise ValueError("tenant not found")
    return tenant


__all__ = [
    "FIXED_TWO_FA_ALREADY_CONFIGURED",
    "FIXED_TWO_FA_NOT_CONFIGURED",
    "get_tenant_fixed_two_fa_settings",
    "set_tenant_fixed_two_fa_password",
    "tenant_fixed_two_fa_password",
]

from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    AccountPool,
    DeveloperAppSlotAssignment,
    Material,
    TelegramDeveloperApp,
    Tenant,
)

from app.services.account_login.contracts import BatchLoginError


FULL_INIT_POLICY = "normal_full_init_v1"
LEGACY_LOGIN_POLICY = "legacy_login_only"
PROFILE_POLICY_VERSION = 1
REQUIRED_ABC_SLOT_PURPOSES = {"primary_sv", "standby_1_sv", "standby_2_my"}


def initialization_policy_for_pool(pool: AccountPool) -> str:
    return FULL_INIT_POLICY if pool.pool_purpose == "normal" else LEGACY_LOGIN_POLICY


def require_post_login_init_ready(session: Session, tenant_id: int, pool: AccountPool) -> None:
    if initialization_policy_for_pool(pool) == LEGACY_LOGIN_POLICY:
        return
    settings = get_settings()
    if settings.account_post_login_init_mode != "enabled":
        raise BatchLoginError(
            "account_post_login_init_disabled",
            "普通账号完整初始化当前不可用",
        )
    tenant = session.get(Tenant, tenant_id)
    configured = bool(
        tenant
        and tenant.fixed_two_fa_password_ciphertext
        and tenant.fixed_two_fa_password_version > 0
    )
    if not configured:
        raise BatchLoginError(
            "tenant_fixed_two_fa_not_configured",
            "租户固定 2FA 尚未完成受保护配置",
        )
    if not _profile_material_ready(session, tenant_id):
        raise BatchLoginError(
            "profile_avatar_material_unavailable",
            "普通账号初始化没有已审核头像素材",
        )
    if not _abc_assignments_ready(session):
        raise BatchLoginError(
            "post_login_abc_assignment_unavailable",
            "ABC Developer App 角色配置不完整",
        )


def _profile_material_ready(session: Session, tenant_id: int) -> bool:
    material_id = session.scalar(
        select(Material.id).where(
            Material.tenant_id == tenant_id,
            Material.material_type == "图片",
            Material.review_status == "已审核",
            Material.source_kind == "upload",
            or_(Material.content != "", Material.cache_ready_status == "ready"),
        ).limit(1)
    )
    return material_id is not None


def _abc_assignments_ready(session: Session) -> bool:
    rows = list(session.execute(
        select(DeveloperAppSlotAssignment, TelegramDeveloperApp)
        .join(
            TelegramDeveloperApp,
            TelegramDeveloperApp.id == DeveloperAppSlotAssignment.developer_app_id,
        )
        .where(
            DeveloperAppSlotAssignment.status == "active",
            DeveloperAppSlotAssignment.slot_purpose.in_(REQUIRED_ABC_SLOT_PURPOSES),
        )
    ))
    purposes = {assignment.slot_purpose for assignment, _app in rows}
    app_ids = {assignment.developer_app_id for assignment, _app in rows}
    credentials_match = all(
        app.is_active and assignment.credentials_version == app.credentials_version
        for assignment, app in rows
    )
    return bool(
        purposes == REQUIRED_ABC_SLOT_PURPOSES
        and len(app_ids) == len(REQUIRED_ABC_SLOT_PURPOSES)
        and credentials_match
    )


__all__ = [
    "FULL_INIT_POLICY",
    "LEGACY_LOGIN_POLICY",
    "PROFILE_POLICY_VERSION",
    "initialization_policy_for_pool",
    "require_post_login_init_ready",
]

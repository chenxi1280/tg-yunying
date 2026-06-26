"""Single-admin auth and captcha routes."""
from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.orm import Session

from app.auth import (
    CurrentUser,
    all_permissions,
    admin_user_payload,
    authenticate_admin,
    authenticate_user,
    consume_captcha_token,
    create_admin_access_token,
    create_access_token,
    create_captcha_challenge,
    format_menu_permissions,
    get_current_user,
    hash_password,
    normalize_permissions,
    normalize_phone,
    require_permission,
    serialize_user,
    verify_password,
    verify_captcha_challenge,
)
from app.database import get_session
from app.models import AppUser, UserTokenLedger
from app.schemas import (
    AdminResetPasswordRequest,
    AdminUserCreate,
    AdminUserOut,
    AdminUserUpdate,
    AuthChangePasswordRequest,
    AuthLoginRequest,
    AuthTokenOut,
    AuthUserOut,
    CaptchaChallengeOut,
    CaptchaVerifyOut,
    CaptchaVerifyRequest,
    TokenAdjustmentRequest,
    UserTokenLedgerOut,
)
from app.services._common import audit

router = APIRouter()

SYSTEM_ADMIN_ROLE = "系统管理员"
SYSTEM_ADMIN_TEMPLATE = "系统管理员"
PRIVILEGED_PERMISSION = "permissions.manage"


@router.get("/api/auth/captcha/challenge", response_model=CaptchaChallengeOut)
def auth_captcha_challenge() -> dict:
    return create_captcha_challenge()


@router.post("/api/auth/captcha/verify", response_model=CaptchaVerifyOut)
def auth_captcha_verify(payload: CaptchaVerifyRequest) -> dict:
    return verify_captcha_challenge(payload.challenge_id, payload.captcha_value)


@router.post("/api/auth/login", response_model=AuthTokenOut)
def auth_login(payload: AuthLoginRequest, session: Session = Depends(get_session)) -> dict:
    consume_captcha_token(payload.captcha_token)
    identifier = (payload.identifier or payload.email or "").strip()
    try:
        user = authenticate_user(session, identifier, payload.password)
    except ProgrammingError as exc:
        if "app_users" not in str(exc):
            raise
        session.rollback()
        user = None
    if user:
        audit(session, tenant_id=user.tenant_id, actor=user.name, action="后台账号登录", target_type="app_user", target_id=str(user.id))
        session.commit()
        return {
            "access_token": create_access_token(user),
            "token_type": "bearer",
            "user": serialize_user(session, user),
        }
    if not authenticate_admin(identifier, payload.password):
        raise HTTPException(status_code=401, detail="invalid identifier or password")
    audit(session, tenant_id=1, actor="bootstrap_admin", action="bootstrap管理员登录", target_type="app_user", target_id="bootstrap")
    session.commit()
    return {
        "access_token": create_admin_access_token(),
        "token_type": "bearer",
        "user": admin_user_payload(),
    }


@router.get("/api/auth/me", response_model=AuthUserOut)
def auth_me(current_user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    return current_user


@router.post("/api/auth/change-password", response_model=AuthUserOut)
def auth_change_password(
    payload: AuthChangePasswordRequest,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> dict:
    user = session.get(AppUser, current_user.id)
    if not user:
        raise HTTPException(status_code=400, detail="bootstrap admin password cannot be changed here")
    if not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(status_code=400, detail="current password is incorrect")
    user.password_hash = hash_password(payload.new_password)
    audit(session, tenant_id=user.tenant_id, actor=current_user.name, action="修改当前用户密码", target_type="app_user", target_id=str(user.id))
    session.commit()
    session.refresh(user)
    return serialize_user(session, user)


@router.get("/api/auth/permissions")
def auth_permissions(_: CurrentUser = Depends(require_permission("permissions.view"))) -> dict[str, list[str]]:
    return {"items": all_permissions()}


@router.post("/api/auth/logout")
def auth_logout(current_user: CurrentUser = Depends(get_current_user), session: Session = Depends(get_session)) -> dict[str, str]:
    audit(session, tenant_id=current_user.tenant_id, actor=current_user.name, action="后台账号登出", target_type="app_user", target_id=str(current_user.id))
    session.commit()
    return {"status": "ok"}


def _admin_user_out(session: Session, user: AppUser) -> dict:
    return serialize_user(session, user) | {
        "created_at": user.created_at,
        "last_login_at": user.last_login_at,
    }


def _active_admin_count(session: Session, *, excluding_user_id: int | None = None) -> int:
    stmt = select(func.count(AppUser.id)).where(AppUser.role == "系统管理员", AppUser.is_active.is_(True))
    if excluding_user_id is not None:
        stmt = stmt.where(AppUser.id != excluding_user_id)
    return int(session.scalar(stmt) or 0)


def _ensure_can_change_admin_safety(session: Session, user: AppUser, payload: AdminUserUpdate) -> None:
    if user.role != "系统管理员" or not user.is_active:
        return
    next_role = payload.role if payload.role is not None else user.role
    next_active = payload.is_active if payload.is_active is not None else user.is_active
    if next_role != "系统管理员" or not next_active:
        if _active_admin_count(session, excluding_user_id=user.id) <= 0:
            raise HTTPException(status_code=400, detail="至少保留一个启用的系统管理员")


def _ensure_admin_identity_available(session: Session, *, email: str | None, phone: str | None, excluding_user_id: int | None = None) -> None:
    conditions = []
    if email:
        conditions.append(AppUser.email == email.strip().lower())
    normalized_phone = normalize_phone(phone)
    if normalized_phone:
        conditions.append(AppUser.phone == normalized_phone)
    if not conditions:
        return
    stmt = select(AppUser.id).where(or_(*conditions))
    if excluding_user_id is not None:
        stmt = stmt.where(AppUser.id != excluding_user_id)
    if session.scalar(stmt):
        raise HTTPException(status_code=400, detail="用户邮箱或手机号已存在")


def _ensure_admin_name_available(session: Session, *, name: str, excluding_user_id: int | None = None) -> None:
    stmt = select(AppUser.id).where(AppUser.name == name.strip())
    if excluding_user_id is not None:
        stmt = stmt.where(AppUser.id != excluding_user_id)
    if session.scalar(stmt):
        raise HTTPException(status_code=400, detail="用户名称已存在")


def _resolve_admin_email(session: Session, email: str | None) -> str:
    if email and email.strip():
        return email.strip().lower()
    for _ in range(5):
        generated = f"user_{secrets.token_hex(8)}@internal.tg-yunying.local"
        if not session.scalar(select(AppUser.id).where(AppUser.email == generated)):
            return generated
    raise HTTPException(status_code=500, detail="生成用户内部标识失败")


def _commit_admin_user_change(session: Session) -> None:
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(status_code=400, detail="用户邮箱或手机号已存在") from exc


def _forbidden_admin_boundary(detail: str) -> None:
    raise HTTPException(status_code=403, detail=detail)


def _final_admin_permissions(*, role: str, role_template: str, permissions: list[str] | None) -> list[str]:
    return normalize_permissions(permissions, role=role, role_template=role_template)


def _ensure_admin_mutation_allowed(
    current_user: CurrentUser,
    *,
    target_user: AppUser | None = None,
    next_role: str,
    next_role_template: str,
    next_permissions: list[str],
    requested_permissions: list[str] | None = None,
) -> None:
    if current_user.is_platform_admin:
        return
    requested_permissions = requested_permissions or []
    if target_user and target_user.role == SYSTEM_ADMIN_ROLE:
        _forbidden_admin_boundary("只有系统管理员可以维护系统管理员账号")
    if next_role == SYSTEM_ADMIN_ROLE or next_role_template == SYSTEM_ADMIN_TEMPLATE or "*" in next_permissions or "*" in requested_permissions:
        _forbidden_admin_boundary("只有系统管理员可以授予系统管理员权限")
    if PRIVILEGED_PERMISSION in next_permissions:
        _forbidden_admin_boundary("非系统管理员不能授予权限管理")


@router.get("/api/admin/users", response_model=list[AdminUserOut])
def list_admin_users(
    session: Session = Depends(get_session),
    _: CurrentUser = Depends(require_permission("permissions.view")),
) -> list[dict]:
    users = session.scalars(select(AppUser).order_by(AppUser.id)).all()
    return [_admin_user_out(session, user) for user in users]


@router.post("/api/admin/users", response_model=AdminUserOut)
def create_admin_user(
    payload: AdminUserCreate,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(require_permission("permissions.manage")),
) -> dict:
    name = payload.name.strip()
    email = _resolve_admin_email(session, payload.email)
    phone = normalize_phone(payload.phone)
    _ensure_admin_name_available(session, name=name)
    _ensure_admin_identity_available(session, email=email, phone=phone)
    permissions = _final_admin_permissions(role=payload.role, role_template=payload.role_template, permissions=payload.permissions)
    _ensure_admin_mutation_allowed(
        current_user,
        next_role=payload.role,
        next_role_template=payload.role_template,
        next_permissions=permissions,
        requested_permissions=payload.permissions,
    )
    user = AppUser(
        tenant_id=current_user.tenant_id or 1,
        name=name,
        role=payload.role,
        role_template=payload.role_template,
        email=email,
        phone=phone,
        password_hash=hash_password(payload.password),
        subscription_status="active",
        menu_permissions=format_menu_permissions(permissions),
        is_active=payload.is_active,
        permission_version=1,
    )
    session.add(user)
    session.flush()
    audit(session, tenant_id=user.tenant_id, actor=current_user.name, action="新增后台账号", target_type="app_user", target_id=str(user.id), detail=f"role={user.role}; template={user.role_template}")
    _commit_admin_user_change(session)
    session.refresh(user)
    return _admin_user_out(session, user)


@router.patch("/api/admin/users/{user_id}", response_model=AdminUserOut)
def update_admin_user(
    user_id: int,
    payload: AdminUserUpdate,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(require_permission("permissions.manage")),
) -> dict:
    user = session.get(AppUser, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="user not found")
    _ensure_can_change_admin_safety(session, user, payload)
    data = payload.model_dump(exclude_unset=True)
    next_role = data.get("role") or user.role
    next_role_template = data.get("role_template") or user.role_template
    permission_values = data.get("permissions", data.get("menu_permissions"))
    next_permissions = (
        _final_admin_permissions(role=next_role, role_template=next_role_template, permissions=permission_values)
        if permission_values is not None or "role" in data or "role_template" in data
        else normalize_permissions(user.menu_permissions.split(",") if user.menu_permissions else None, role=user.role, role_template=user.role_template)
    )
    _ensure_admin_mutation_allowed(
        current_user,
        target_user=user,
        next_role=next_role,
        next_role_template=next_role_template,
        next_permissions=next_permissions,
        requested_permissions=permission_values,
    )
    if "email" in data and data["email"] is not None:
        next_email = data["email"].strip().lower()
        _ensure_admin_identity_available(session, email=next_email, phone=None, excluding_user_id=user.id)
        user.email = next_email
    if "phone" in data:
        next_phone = normalize_phone(data["phone"])
        _ensure_admin_identity_available(session, email=None, phone=next_phone, excluding_user_id=user.id)
        user.phone = next_phone
    if "name" in data and data["name"] is not None:
        next_name = data["name"].strip()
        _ensure_admin_name_available(session, name=next_name, excluding_user_id=user.id)
        user.name = next_name
    for field in ["role", "role_template", "subscription_status", "is_active"]:
        if field in data and data[field] is not None:
            setattr(user, field, data[field].strip() if isinstance(data[field], str) else data[field])
    permissions = data.get("permissions", data.get("menu_permissions"))
    if permissions is not None or "role" in data or "role_template" in data:
        user.menu_permissions = format_menu_permissions(normalize_permissions(permissions, role=user.role, role_template=user.role_template))
    user.permission_version += 1
    audit(session, tenant_id=user.tenant_id, actor=current_user.name, action="更新后台账号权限", target_type="app_user", target_id=str(user.id), detail=f"role={user.role}; template={user.role_template}; active={user.is_active}; permission_version={user.permission_version}")
    _commit_admin_user_change(session)
    session.refresh(user)
    return _admin_user_out(session, user)


@router.post("/api/admin/users/{user_id}/reset-password", response_model=AdminUserOut)
def reset_admin_user_password(
    user_id: int,
    payload: AdminResetPasswordRequest,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(require_permission("permissions.manage")),
) -> dict:
    user = session.get(AppUser, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="user not found")
    _ensure_admin_mutation_allowed(
        current_user,
        target_user=user,
        next_role=user.role,
        next_role_template=user.role_template,
        next_permissions=normalize_permissions(user.menu_permissions.split(",") if user.menu_permissions else None, role=user.role, role_template=user.role_template),
    )
    user.password_hash = hash_password(payload.new_password)
    user.permission_version += 1
    audit(session, tenant_id=user.tenant_id, actor=current_user.name, action="重置后台账号密码", target_type="app_user", target_id=str(user.id))
    session.commit()
    session.refresh(user)
    return _admin_user_out(session, user)


@router.get("/api/admin/users/{user_id}/token-ledgers", response_model=list[UserTokenLedgerOut])
def get_admin_user_token_ledgers(
    user_id: int,
    session: Session = Depends(get_session),
    _: CurrentUser = Depends(require_permission("permissions.view")),
) -> list[UserTokenLedger]:
    return list(session.scalars(select(UserTokenLedger).where(UserTokenLedger.user_id == user_id).order_by(UserTokenLedger.id.desc()).limit(50)))


@router.post("/api/admin/users/{user_id}/token-adjustments", response_model=AdminUserOut)
def adjust_admin_user_tokens(
    user_id: int,
    payload: TokenAdjustmentRequest,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(require_permission("permissions.manage")),
) -> dict:
    user = session.get(AppUser, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="user not found")
    _ensure_admin_mutation_allowed(
        current_user,
        target_user=user,
        next_role=user.role,
        next_role_template=user.role_template,
        next_permissions=normalize_permissions(user.menu_permissions.split(",") if user.menu_permissions else None, role=user.role, role_template=user.role_template),
    )
    user.token_balance += payload.delta_tokens
    user.token_quota_total += max(payload.delta_tokens, 0)
    session.add(
        UserTokenLedger(
            tenant_id=user.tenant_id,
            user_id=user.id,
            change_type="管理员调整",
            delta_tokens=payload.delta_tokens,
            balance_after=user.token_balance,
            reason=payload.reason,
            actor=current_user.name,
        )
    )
    audit(session, tenant_id=user.tenant_id, actor=current_user.name, action="调整后台账号Token", target_type="app_user", target_id=str(user.id), detail=f"delta={payload.delta_tokens}; reason={payload.reason}")
    session.commit()
    session.refresh(user)
    return _admin_user_out(session, user)

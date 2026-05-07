"""Auth, captcha, subscription, and admin routes."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.auth import (
    CurrentUser, authenticate_user, consume_captcha_token,
    create_captcha_challenge, create_access_token, get_current_user,
    hash_password, is_legacy_password_hash, serialize_user, verify_captcha_challenge,
)
from app.database import get_session
from app.common.http import forbidden
from app.schemas import (
    ActivationCodeCreateRequest, ActivationCodeOut, ActivationCodePageOut, AdminResetPasswordRequest,
    AdminUserOut, AdminUserUpdate, AiUsageLedgerOut, AiUsageSummaryOut,
    AuthChangePasswordRequest, AuthLoginRequest, AuthRegisterRequest, AuthTokenOut,
    AuthUserOut, CaptchaChallengeOut, CaptchaVerifyOut, CaptchaVerifyRequest,
    SubscriptionPlanCreate, SubscriptionPlanOut, SubscriptionPlanUpdate,
    SubscriptionRedeemOut, SubscriptionRedeemRequest, TokenAdjustmentRequest, UserTokenLedgerOut,
)
from app.services import (
    adjust_user_tokens, change_user_password, create_subscription_plan,
    create_user_activation_codes, create_user_registration, disable_activation_code,
    list_activation_codes, list_admin_users, list_subscription_plans, list_usage_ledgers,
    list_usage_summary, list_user_token_ledgers, redeem_activation_code,
    reset_admin_user_password, update_admin_user, update_subscription_plan,
)

router = APIRouter()


@router.get("/api/auth/captcha/challenge", response_model=CaptchaChallengeOut)
def auth_captcha_challenge() -> dict:
    return create_captcha_challenge()


@router.post("/api/auth/captcha/verify", response_model=CaptchaVerifyOut)
def auth_captcha_verify(payload: CaptchaVerifyRequest) -> dict:
    return verify_captcha_challenge(payload.challenge_id, payload.captcha_value)


@router.post("/api/auth/register", response_model=AuthTokenOut)
def auth_register(payload: AuthRegisterRequest, session: Session = Depends(get_session)) -> dict:
    consume_captcha_token(payload.captcha_token)
    user = create_user_registration(session, payload)
    return {
        "access_token": create_access_token(user),
        "token_type": "bearer",
        "user": serialize_user(session, user),
    }


@router.post("/api/auth/login", response_model=AuthTokenOut)
def auth_login(payload: AuthLoginRequest, session: Session = Depends(get_session)) -> dict:
    consume_captcha_token(payload.captcha_token)
    identifier = (payload.identifier or payload.email or "").strip()
    user = authenticate_user(session, identifier, payload.password)
    if not user:
        raise HTTPException(status_code=401, detail="invalid identifier or password")
    # 自动升级旧格式密码哈希为新格式（独立 salt + 600k 迭代）
    if is_legacy_password_hash(user.password_hash):
        user.password_hash = hash_password(payload.password)
        session.commit()
    return {
        "access_token": create_access_token(user),
        "token_type": "bearer",
        "user": serialize_user(session, user),
    }


@router.get("/api/auth/me", response_model=AuthUserOut)
def auth_me(current_user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    return current_user


@router.post("/api/auth/logout")
def auth_logout() -> dict[str, str]:
    return {"status": "ok"}


@router.post("/api/auth/change-password", response_model=AuthUserOut)
def auth_change_password(
    payload: AuthChangePasswordRequest,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    try:
        user = change_user_password(session, current_user, payload.current_password, payload.new_password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return serialize_user(session, user)


@router.post("/api/subscription/redeem", response_model=SubscriptionRedeemOut)
def post_subscription_redeem(
    payload: SubscriptionRedeemRequest,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> dict:
    try:
        return redeem_activation_code(session, current_user, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/admin/activation-codes", response_model=ActivationCodePageOut)
def get_activation_codes(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    search: str | None = None,
    status: str | None = None,
    plan_type: str | None = None,
    batch_no: str | None = None,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    if not current_user.is_platform_admin:
        raise forbidden("platform admin required")
    return list_activation_codes(session, page=page, page_size=page_size, search=search, status=status, plan_type=plan_type, batch_no=batch_no, start_at=start_at, end_at=end_at)


@router.get("/api/admin/subscription-plans", response_model=list[SubscriptionPlanOut])
def get_subscription_plans(
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    if not current_user.is_platform_admin:
        raise forbidden("platform admin required")
    return list_subscription_plans(session)


@router.post("/api/admin/subscription-plans", response_model=SubscriptionPlanOut)
def post_subscription_plan(
    payload: SubscriptionPlanCreate,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    if not current_user.is_platform_admin:
        raise forbidden("platform admin required")
    try:
        return create_subscription_plan(session, payload, current_user.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/api/admin/subscription-plans/{plan_id}", response_model=SubscriptionPlanOut)
def patch_subscription_plan(
    plan_id: int,
    payload: SubscriptionPlanUpdate,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    if not current_user.is_platform_admin:
        raise forbidden("platform admin required")
    try:
        return update_subscription_plan(session, plan_id, payload, current_user.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/admin/users", response_model=list[AdminUserOut])
def get_admin_users(
    search: str | None = None,
    role: str | None = None,
    tenant_id: int | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    if not current_user.is_platform_admin:
        raise forbidden("platform admin required")
    return list_admin_users(session, search=search, role=role, tenant_id=tenant_id)


@router.patch("/api/admin/users/{user_id}", response_model=AdminUserOut)
def patch_admin_user(
    user_id: int,
    payload: AdminUserUpdate,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    if not current_user.is_platform_admin:
        raise forbidden("platform admin required")
    try:
        return update_admin_user(session, user_id, payload, current_user.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/admin/users/{user_id}/reset-password", response_model=AdminUserOut)
def post_admin_user_reset_password(
    user_id: int,
    payload: AdminResetPasswordRequest,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    if not current_user.is_platform_admin:
        raise forbidden("platform admin required")
    try:
        return reset_admin_user_password(session, user_id, payload.new_password, current_user.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/admin/users/{user_id}/token-adjustments", response_model=AdminUserOut)
def post_admin_user_token_adjustment(
    user_id: int,
    payload: TokenAdjustmentRequest,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    if not current_user.is_platform_admin:
        raise forbidden("platform admin required")
    try:
        return adjust_user_tokens(session, user_id, payload, current_user.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/admin/users/{user_id}/token-ledgers", response_model=list[UserTokenLedgerOut])
def get_admin_user_token_ledgers(
    user_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    if not current_user.is_platform_admin:
        raise forbidden("platform admin required")
    try:
        return list_user_token_ledgers(session, user_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/admin/activation-codes", response_model=list[ActivationCodeOut])
def post_activation_codes(
    payload: ActivationCodeCreateRequest,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    if not current_user.is_platform_admin:
        raise forbidden("platform admin required")
    return create_user_activation_codes(session, payload, current_user.name)


@router.post("/api/admin/activation-codes/{code_id}/disable", response_model=ActivationCodeOut)
def post_activation_code_disable(
    code_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    if not current_user.is_platform_admin:
        raise forbidden("platform admin required")
    try:
        return disable_activation_code(session, code_id, current_user.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/admin/usage-ledgers", response_model=list[AiUsageLedgerOut])
def get_usage_ledgers(
    user_id: int | None = None,
    campaign_id: int | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    if not current_user.is_platform_admin:
        raise forbidden("platform admin required")
    return list_usage_ledgers(session, user_id=user_id, campaign_id=campaign_id)


@router.get("/api/admin/usage-summary", response_model=AiUsageSummaryOut)
def get_usage_summary(
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> dict:
    if not current_user.is_platform_admin:
        raise forbidden("platform admin required")
    return list_usage_summary(session)

"""Auth, captcha, subscription, and admin routes."""
from __future__ import annotations


from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.auth import (
    CurrentUser, authenticate_user, consume_captcha_token,
    create_captcha_challenge, create_access_token, get_current_user,
    serialize_user, verify_captcha_challenge,
)
from app.database import get_session
from app.common.http import forbidden
from app.schemas import (
    ActivationCodeCreateRequest, ActivationCodeOut, AiUsageLedgerOut,
    AiUsageSummaryOut, AuthLoginRequest, AuthRegisterRequest, AuthTokenOut,
    AuthUserOut, CaptchaChallengeOut, CaptchaVerifyOut, CaptchaVerifyRequest,
    SubscriptionRedeemOut, SubscriptionRedeemRequest,
)
from app.services import (
    create_user_activation_codes, create_user_registration, list_activation_codes,
    list_usage_ledgers, list_usage_summary, redeem_activation_code,
)

router = APIRouter()


@router.get("/api/auth/captcha/challenge", response_model=CaptchaChallengeOut)
def auth_captcha_challenge() -> dict:
    return create_captcha_challenge()


@router.post("/api/auth/captcha/verify", response_model=CaptchaVerifyOut)
def auth_captcha_verify(payload: CaptchaVerifyRequest) -> dict:
    return verify_captcha_challenge(payload.challenge_id, payload.slider_value)


@router.post("/api/auth/register", response_model=AuthTokenOut)
def auth_register(payload: AuthRegisterRequest, session: Session = Depends(get_session)) -> dict:
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


@router.post("/api/subscription/redeem", response_model=SubscriptionRedeemOut)
def post_subscription_redeem(
    payload: SubscriptionRedeemRequest,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> dict:
    return redeem_activation_code(session, current_user, payload)


@router.get("/api/admin/activation-codes", response_model=list[ActivationCodeOut])
def get_activation_codes(
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    if not current_user.is_platform_admin:
        raise forbidden("platform admin required")
    return list_activation_codes(session)


@router.post("/api/admin/activation-codes", response_model=list[ActivationCodeOut])
def post_activation_codes(
    payload: ActivationCodeCreateRequest,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    if not current_user.is_platform_admin:
        raise forbidden("platform admin required")
    return create_user_activation_codes(session, payload, current_user.name)


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

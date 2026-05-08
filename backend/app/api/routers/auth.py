"""Single-admin auth and captcha routes."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.auth import (
    CurrentUser,
    admin_user_payload,
    authenticate_admin,
    consume_captcha_token,
    create_admin_access_token,
    create_captcha_challenge,
    get_current_user,
    verify_captcha_challenge,
)
from app.schemas import (
    AuthLoginRequest,
    AuthTokenOut,
    AuthUserOut,
    CaptchaChallengeOut,
    CaptchaVerifyOut,
    CaptchaVerifyRequest,
)

router = APIRouter()


@router.get("/api/auth/captcha/challenge", response_model=CaptchaChallengeOut)
def auth_captcha_challenge() -> dict:
    return create_captcha_challenge()


@router.post("/api/auth/captcha/verify", response_model=CaptchaVerifyOut)
def auth_captcha_verify(payload: CaptchaVerifyRequest) -> dict:
    return verify_captcha_challenge(payload.challenge_id, payload.captcha_value)


@router.post("/api/auth/login", response_model=AuthTokenOut)
def auth_login(payload: AuthLoginRequest) -> dict:
    consume_captcha_token(payload.captcha_token)
    identifier = (payload.identifier or payload.email or "").strip()
    if not authenticate_admin(identifier, payload.password):
        raise HTTPException(status_code=401, detail="invalid identifier or password")
    return {
        "access_token": create_admin_access_token(),
        "token_type": "bearer",
        "user": admin_user_payload(),
    }


@router.get("/api/auth/me", response_model=AuthUserOut)
def auth_me(current_user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    return current_user


@router.post("/api/auth/logout")
def auth_logout() -> dict[str, str]:
    return {"status": "ok"}

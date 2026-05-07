from __future__ import annotations
from datetime import datetime

from pydantic import BaseModel, Field

from .api import ApiModel


# ── Tenant ──

class TenantCreate(BaseModel):
    name: str
    plan_name: str = "试运行"
    account_quota: int = 50
    task_quota: int = 5000


class TenantUpdate(BaseModel):
    name: str | None = None
    plan_name: str | None = None
    account_quota: int | None = Field(default=None, ge=0)
    task_quota: int | None = Field(default=None, ge=0)


class TenantOut(ApiModel):
    id: int
    name: str
    plan_name: str
    account_quota: int
    task_quota: int
    created_at: datetime


# ── Auth ──

class AuthLoginRequest(BaseModel):
    identifier: str | None = None
    email: str | None = None
    password: str
    captcha_token: str


class AuthRegisterRequest(BaseModel):
    name: str = Field(..., min_length=2, max_length=80)
    email: str = Field(..., min_length=3, max_length=160)
    phone: str | None = Field(default=None, max_length=40)
    password: str = Field(..., min_length=6, max_length=80)
    captcha_token: str


class AuthUserOut(BaseModel):
    id: int
    tenant_id: int | None
    name: str
    role: str
    email: str
    phone: str | None = None
    tenant_name: str | None = None
    subscription_status: str
    subscription_started_at: datetime | None = None
    subscription_expires_at: datetime | None = None
    subscription_days_remaining: int = 0
    can_use_core_features: bool = True


class AuthTokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: AuthUserOut


# ── Captcha ──

class CaptchaChallengeOut(BaseModel):
    challenge_id: str
    slider_min: int
    slider_max: int
    target_value: int
    expires_at: datetime


class CaptchaVerifyRequest(BaseModel):
    challenge_id: str
    slider_value: int


class CaptchaVerifyOut(BaseModel):
    captcha_token: str
    expires_at: datetime


# ── Subscription / Activation ──

class SubscriptionRedeemRequest(BaseModel):
    code: str = Field(..., min_length=6, max_length=64)


class SubscriptionRedeemOut(BaseModel):
    subscription_status: str
    subscription_started_at: datetime | None
    subscription_expires_at: datetime | None
    subscription_days_remaining: int
    activation_code: str
    plan_type: str
    duration_days: int
    redeemed_at: datetime


class ActivationCodeCreateRequest(BaseModel):
    plan_type: str  # Literal["monthly", "yearly"] — relaxed to str for validation flexibility
    quantity: int = Field(default=1, ge=1, le=200)
    note: str = Field(default="", max_length=255)


class ActivationCodeOut(ApiModel):
    id: int
    code: str
    plan_type: str
    duration_days: int
    status: str
    created_by: str
    created_at: datetime
    redeemed_by_user_id: int | None
    redeemed_at: datetime | None
    subscription_start_at: datetime | None
    subscription_end_at: datetime | None
    note: str


# ── Runtime config (system info exposed to authorised users) ──

class RuntimeConfigOut(BaseModel):
    app_env: str
    queue_backend: str
    tg_gateway_mode: str
    telethon_configured: bool
    sync_dispatch_fallback: bool
    code_ttl_seconds: int
    developer_app_pool_enabled: bool
    developer_app_count: int
    developer_app_healthy_count: int
    ai_enabled: bool
    ai_provider_count: int
    healthy_ai_provider_count: int
    mock_ai_fallback_enabled: bool
    avatar_max_bytes: int
    avatar_allowed_types: list[str]
    show_advanced_debug: bool = False


__all__ = [
    "TenantCreate", "TenantUpdate", "TenantOut",
    "AuthLoginRequest", "AuthRegisterRequest", "AuthUserOut", "AuthTokenOut",
    "CaptchaChallengeOut", "CaptchaVerifyRequest", "CaptchaVerifyOut",
    "SubscriptionRedeemRequest", "SubscriptionRedeemOut",
    "ActivationCodeCreateRequest", "ActivationCodeOut",
    "RuntimeConfigOut",
]

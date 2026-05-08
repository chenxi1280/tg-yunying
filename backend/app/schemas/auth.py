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
    admin_chat_id: str = ""
    notify_ai_failures_enabled: bool = False
    telegram_bot_configured: bool = False
    created_at: datetime


class TenantNotificationSettingsOut(BaseModel):
    tenant_id: int
    notify_ai_failures_enabled: bool = False
    admin_chat_id: str = ""
    telegram_bot_configured: bool = False


class TenantNotificationSettingsUpdate(BaseModel):
    notify_ai_failures_enabled: bool | None = None
    admin_chat_id: str | None = Field(default=None, max_length=120)
    telegram_bot_token: str | None = Field(default=None, max_length=300)


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


class AuthChangePasswordRequest(BaseModel):
    current_password: str = Field(..., min_length=1, max_length=80)
    new_password: str = Field(..., min_length=6, max_length=80)


class AuthUserOut(BaseModel):
    id: int
    tenant_id: int | None
    name: str
    role: str
    email: str
    phone: str | None = None
    tenant_name: str | None = None
    can_use_core_features: bool = True


class AuthTokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: AuthUserOut


# ── Captcha ──

class CaptchaChallengeOut(BaseModel):
    challenge_id: str
    image_data_url: str
    expires_at: datetime


class CaptchaVerifyRequest(BaseModel):
    challenge_id: str
    captcha_value: str


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
    plan_name: str = ""
    duration_days: int
    token_quota: int = 0
    token_balance: int = 0
    redeemed_at: datetime


class SubscriptionPlanCreate(BaseModel):
    plan_type: str = Field(..., min_length=1, max_length=30, pattern=r"^[A-Za-z0-9_-]+$")
    name: str = Field(..., min_length=1, max_length=80)
    duration_days: int = Field(..., ge=1, le=3650)
    token_quota: int = Field(default=0, ge=0)
    is_active: bool = True
    note: str = Field(default="", max_length=255)


class SubscriptionPlanUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    duration_days: int | None = Field(default=None, ge=1, le=3650)
    token_quota: int | None = Field(default=None, ge=0)
    is_active: bool | None = None
    note: str | None = Field(default=None, max_length=255)


class SubscriptionPlanOut(ApiModel):
    id: int
    plan_type: str
    name: str
    duration_days: int
    token_quota: int
    is_active: bool
    note: str
    created_at: datetime
    updated_at: datetime


class ActivationCodeCreateRequest(BaseModel):
    plan_type: str  # Literal["monthly", "yearly"] — relaxed to str for validation flexibility
    plan_id: int | None = None
    quantity: int = Field(default=1, ge=1, le=200)
    batch_no: str = Field(default="", max_length=24, pattern=r"^[A-Za-z0-9_-]*$")
    serial_prefix: str = Field(default="", max_length=24, pattern=r"^[A-Za-z0-9_-]*$")
    note: str = Field(default="", max_length=255)


class ActivationCodeOut(ApiModel):
    id: int
    code: str
    plan_id: int | None = None
    plan_type: str
    plan_name: str = ""
    duration_days: int
    token_quota: int = 0
    status: str
    batch_no: str
    serial_prefix: str
    created_by: str
    created_at: datetime
    redeemed_by_user_id: int | None
    redeemed_user_name: str | None = None
    redeemed_user_email: str | None = None
    redeemed_at: datetime | None
    subscription_start_at: datetime | None
    subscription_end_at: datetime | None
    note: str


class ActivationCodePageOut(BaseModel):
    items: list[ActivationCodeOut]
    total: int
    page: int
    page_size: int


class AdminUserOut(BaseModel):
    id: int
    tenant_id: int | None
    tenant_name: str | None = None
    name: str
    role: str
    email: str
    phone: str | None = None
    subscription_status: str
    subscription_started_at: datetime | None = None
    subscription_expires_at: datetime | None = None
    subscription_days_remaining: int = 0
    token_balance: int = 0
    token_quota_total: int = 0
    menu_permissions: list[str] = []
    is_active: bool
    created_at: datetime
    last_login_at: datetime | None = None


class AdminUserUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    email: str | None = Field(default=None, min_length=3, max_length=160)
    phone: str | None = Field(default=None, max_length=40)
    role: str | None = Field(default=None, pattern="^(系统管理员|普通用户)$")
    subscription_status: str | None = Field(default=None, max_length=30)
    menu_permissions: list[str] | None = None
    is_active: bool | None = None


class AdminResetPasswordRequest(BaseModel):
    new_password: str = Field(..., min_length=6, max_length=80)


class TokenAdjustmentRequest(BaseModel):
    delta_tokens: int
    reason: str = Field(default="管理员调整", max_length=255)


class UserTokenLedgerOut(ApiModel):
    id: int
    tenant_id: int | None
    user_id: int
    change_type: str
    delta_tokens: int
    balance_after: int
    related_activation_code_id: int | None
    related_ai_usage_ledger_id: int | None
    reason: str
    actor: str
    created_at: datetime


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
    can_create_tg_account: bool = False
    has_ai_provider: bool = False
    ai_enabled: bool
    ai_provider_count: int
    healthy_ai_provider_count: int
    mock_ai_fallback_enabled: bool
    avatar_max_bytes: int
    avatar_allowed_types: list[str]
    show_advanced_debug: bool = False


__all__ = [
    "TenantCreate", "TenantUpdate", "TenantOut", "TenantNotificationSettingsOut", "TenantNotificationSettingsUpdate",
    "AuthLoginRequest", "AuthRegisterRequest", "AuthChangePasswordRequest", "AuthUserOut", "AuthTokenOut",
    "CaptchaChallengeOut", "CaptchaVerifyRequest", "CaptchaVerifyOut",
    "SubscriptionRedeemRequest", "SubscriptionRedeemOut",
    "SubscriptionPlanCreate", "SubscriptionPlanUpdate", "SubscriptionPlanOut",
    "ActivationCodeCreateRequest", "ActivationCodeOut", "ActivationCodePageOut",
    "AdminUserOut", "AdminUserUpdate", "AdminResetPasswordRequest", "TokenAdjustmentRequest", "UserTokenLedgerOut",
    "RuntimeConfigOut",
]

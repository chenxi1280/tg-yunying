from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field
from .api import ApiModel


class RiskControlMetricOut(ApiModel):
    key: str
    label: str
    value: int | float | str
    detail: str = ""
    status: str = "normal"


class RiskControlOverviewOut(ApiModel):
    current_level: str
    level_detail: str
    quiet_active: bool
    metrics: list[RiskControlMetricOut]


class RiskControlGlobalPolicyOut(ApiModel):
    jitter_min_seconds: int
    jitter_max_seconds: int
    batch_interval_seconds: int
    respect_send_window: bool
    quiet_hours_enabled: bool
    quiet_start: str
    quiet_end: str
    quiet_timezone: str
    default_max_retries: int
    default_retry_delay_seconds: int
    default_retry_backoff: str
    default_on_account_banned: str
    default_on_api_rate_limit: str
    default_on_content_rejected: str
    default_account_hour_limit: int
    default_account_day_limit: int
    default_account_cooldown_seconds: int
    updated_at: datetime


class RiskControlGlobalPolicyUpdate(BaseModel):
    jitter_min_seconds: int | None = Field(default=None, ge=0, le=86400)
    jitter_max_seconds: int | None = Field(default=None, ge=0, le=86400)
    batch_interval_seconds: int | None = Field(default=None, ge=0, le=86400)
    respect_send_window: bool | None = None
    quiet_hours_enabled: bool | None = None
    quiet_start: str | None = None
    quiet_end: str | None = None
    quiet_timezone: str | None = None
    default_max_retries: int | None = Field(default=None, ge=0, le=20)
    default_retry_delay_seconds: int | None = Field(default=None, ge=0, le=86400)
    default_retry_backoff: str | None = None
    default_on_account_banned: str | None = None
    default_on_api_rate_limit: str | None = None
    default_on_content_rejected: str | None = None
    default_account_hour_limit: int | None = Field(default=None, ge=0, le=100000)
    default_account_day_limit: int | None = Field(default=None, ge=0, le=100000)
    default_account_cooldown_seconds: int | None = Field(default=None, ge=0, le=86400)


class RiskControlAccountScoreOut(ApiModel):
    account_id: int
    display_name: str
    username: str | None = None
    phone_masked: str
    phone_number: str | None = None
    pool_name: str
    login_status: str
    health_score: float
    risk_level: str
    current_policy: str
    hour_usage: int
    hour_limit: int
    day_usage: int
    day_limit: int
    cooldown_until: datetime | None = None
    recent_risk: str = ""
    blocked_reason: str = ""
    score_reasons: list[str] = Field(default_factory=list)
    proxy_id: int | None = None
    proxy_name: str | None = None
    proxy_local_address: str | None = None
    proxy_status: str | None = None
    proxy_alert_status: str | None = None
    proxy_risk_reason: str = ""
    trusted_session_status: str = "unknown"
    two_fa_status: str = "unknown"
    external_authorization_count: int = 0
    security_profile_status: str = "unknown"
    security_risk_reason: str = ""
    can_join_task: bool


class RiskDispositionItemOut(ApiModel):
    key: str
    item_type: str
    severity: str
    account_id: int | None = None
    account_name: str = ""
    target: str = ""
    reason: str
    suggested_action: str
    occurred_at: datetime | None = None
    status: str = "待处理"


class RiskHitRecordOut(ApiModel):
    key: str
    source: str
    severity: str
    account_id: int | None = None
    account_name: str = ""
    task_id: str = ""
    target: str = ""
    policy: str
    action: str
    detail: str
    occurred_at: datetime | None = None


class RiskProxyAlertOut(ApiModel):
    id: int | None = None
    proxy_id: int | None = None
    name: str
    local_address: str
    alert_status: str
    severity: str
    alert_type: str = ""
    reason_code: str = ""
    bound_accounts: int
    last_error: str = ""
    suggested_action: str = ""
    occurred_at: datetime | None = None


class AccountProxyCreate(BaseModel):
    name: str
    protocol: str = Field(default="socks5", pattern="^(socks5|http)$")
    host: str = "127.0.0.1"
    port: int = Field(ge=1, le=65535)
    username: str = ""
    password: str = ""
    check_interval_seconds: int = Field(default=300, ge=30, le=86400)
    timeout_ms: int = Field(default=3000, ge=100, le=60000)
    max_bound_accounts: int = Field(default=5, ge=0, le=1000)
    max_concurrent_sessions: int = Field(default=2, ge=0, le=1000)
    notes: str = ""


class AccountProxyUpdate(BaseModel):
    name: str | None = None
    protocol: str | None = Field(default=None, pattern="^(socks5|http)$")
    host: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    username: str | None = None
    password_reset: str | None = None
    check_interval_seconds: int | None = Field(default=None, ge=30, le=86400)
    timeout_ms: int | None = Field(default=None, ge=100, le=60000)
    max_bound_accounts: int | None = Field(default=None, ge=0, le=1000)
    max_concurrent_sessions: int | None = Field(default=None, ge=0, le=1000)
    notes: str | None = None
    change_reason: str = ""


class AccountProxyOut(ApiModel):
    id: int
    tenant_id: int
    name: str
    protocol: str
    host: str
    port: int
    username: str
    status: str
    alert_status: str
    check_interval_seconds: int
    timeout_ms: int
    max_bound_accounts: int
    max_concurrent_sessions: int
    last_check_at: datetime | None = None
    last_error: str = ""
    disabled_reason: str = ""
    notes: str = ""
    local_address: str
    bound_account_count: int = 0
    created_at: datetime
    updated_at: datetime
    trace_id: str = ""


class ProxyHealthCheckOut(ApiModel):
    id: int
    proxy_id: int
    check_type: str
    status: str
    latency_ms: int
    error_code: str
    error_detail: str
    checked_by: str
    checked_at: datetime
    trace_id: str
    related_alert_id: int | None = None


class ProxyCheckRequest(BaseModel):
    check_type: str = "quick"
    reason: str = ""


class ProxyDisableRequest(BaseModel):
    disabled_reason: str


class ProxyAlertActionRequest(BaseModel):
    reason: str = ""
    ignored_until: datetime | None = None


class ProxyBindingRequest(BaseModel):
    proxy_id: int | None = None
    change_reason: str
    run_precheck: bool = True


class ProxyBindingOut(ApiModel):
    account_id: int
    old_proxy_id: int | None = None
    new_proxy_id: int | None = None
    proxy_status: str = ""
    proxy_alert_status: str = ""
    affected_pending_action_count: int = 0
    affected_running_action_count: int = 0
    warnings: list[str] = Field(default_factory=list)
    trace_id: str
    audit_id: str = ""


class ProxyBatchBindingRequest(BaseModel):
    account_ids: list[int]
    proxy_id: int | None = None
    assignment_mode: str = "same_proxy"
    manual_bindings: dict[str, int | None] = Field(default_factory=dict)
    change_reason: str
    run_precheck: bool = True


class ProxyBatchBindingOut(ApiModel):
    success_count: int
    failed_count: int
    skipped_accounts: list[dict[str, Any]]
    affected_account_ids: list[int]
    affected_pending_action_count: int
    affected_running_action_count: int
    warnings: list[str]
    trace_id: str
    audit_id: str = ""


class RiskPreflightRequest(BaseModel):
    scenario: str = "manual_send"
    account_ids: list[int] = Field(default_factory=list)
    proxy_ids: list[int] = Field(default_factory=list)
    target_ids: list[int] = Field(default_factory=list)
    content_preview: str = ""
    task_type: str = ""
    scheduled_at: datetime | None = None


class RiskPreflightOut(ApiModel):
    decision: str
    available_accounts: list[dict[str, Any]]
    limited_accounts: list[dict[str, Any]]
    blocked_accounts: list[dict[str, Any]]
    proxy_decisions: list[dict[str, Any]]
    proxy_warnings: list[str]
    proxy_alerts: list[dict[str, Any]]
    target_warnings: list[str]
    content_warnings: list[str]
    suggested_actions: list[str]
    decision_reasons: list[str]
    expires_at: datetime
    trace_id: str


class RiskControlSummaryOut(ApiModel):
    overview: RiskControlOverviewOut
    global_policy: RiskControlGlobalPolicyOut
    account_scores: list[RiskControlAccountScoreOut]
    disposition_queue: list[RiskDispositionItemOut]
    hit_records: list[RiskHitRecordOut]
    proxy_alerts: list[RiskProxyAlertOut]


__all__ = [
    "RiskControlAccountScoreOut",
    "RiskControlGlobalPolicyOut",
    "RiskControlGlobalPolicyUpdate",
    "RiskControlMetricOut",
    "RiskControlOverviewOut",
    "RiskControlSummaryOut",
    "RiskDispositionItemOut",
    "RiskHitRecordOut",
    "RiskProxyAlertOut",
    "AccountProxyCreate",
    "AccountProxyOut",
    "AccountProxyUpdate",
    "ProxyAlertActionRequest",
    "ProxyBatchBindingOut",
    "ProxyBatchBindingRequest",
    "ProxyBindingOut",
    "ProxyBindingRequest",
    "ProxyCheckRequest",
    "ProxyDisableRequest",
    "ProxyHealthCheckOut",
    "RiskPreflightOut",
    "RiskPreflightRequest",
]

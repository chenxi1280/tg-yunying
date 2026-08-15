from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .api import ApiModel


class _ReasonRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=255)

    @field_validator("reason")
    @classmethod
    def require_reason_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("操作原因不能为空")
        return normalized


class LoginBatchPrecheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pool_id: int
    lines_text: str = Field(min_length=1)


class LoginBatchBindingDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    line_no: int
    replace_binding: bool = False
    expected_binding_version: int | None = None


class LoginBatchCreateRequest(LoginBatchPrecheckRequest):
    binding_decisions: list[LoginBatchBindingDecision] = Field(default_factory=list)
    preview_token: str = Field(min_length=1)
    preview_fingerprint: str = Field(min_length=64, max_length=64)
    idempotency_key: str = Field(min_length=8, max_length=80)
    reason: str = Field(min_length=1, max_length=255)

    @model_validator(mode="after")
    def normalize_reason(self) -> "LoginBatchCreateRequest":
        self.reason = self.reason.strip()
        if not self.reason:
            raise ValueError("操作原因不能为空")
        return self


class LoginBatchPrecheckItemOut(BaseModel):
    line_no: int
    phone_masked: str
    route_hint: str
    account_id: int | None = None
    current_pool_id: int | None = None
    code_source_note: str
    binding_action: str = "bind"
    current_binding_note: str = ""
    current_binding_version: int = 0


class LoginBatchPrecheckOut(BaseModel):
    preview_token: str
    preview_fingerprint: str
    expires_at: datetime
    total_count: int
    create_count: int
    existing_probe_required_count: int
    migrate_count: int
    queue_position: int
    estimated_seconds: int
    worst_case_seconds: int
    credential_expires_at: datetime
    items: list[LoginBatchPrecheckItemOut]


class LoginBatchItemOut(ApiModel):
    id: int
    batch_id: int
    line_no: int
    phone_masked: str
    code_source_host: str
    code_source_uuid_hint: str
    code_source_note: str = ""
    route_hint: str
    route: str
    account_id: int | None
    status: str
    phase: str
    failure_type: str
    failure_detail: str
    warning_detail: str
    current_attempt_id: int | None
    current_attempt_state_version: int = 0
    reconcile_status: str = "none"
    reconcile_attempted: bool = False
    account_binding_version: int = 0
    execution_generation: int
    retry_count: int
    state_version: int
    next_retry_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    updated_at: datetime


class LoginBatchOut(ApiModel):
    id: int
    tenant_id: int
    pool_id: int
    created_by: str
    status: str
    state_version: int
    execution_generation: int
    resolution_version: int
    total_count: int
    success_count: int
    failed_count: int
    unresolved_count: int
    warning_count: int
    skipped_count: int
    reason: str
    trace_id: str
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime


class LoginBatchDetailOut(LoginBatchOut):
    items: list[LoginBatchItemOut] = Field(default_factory=list)


class LoginBatchRetryRequest(_ReasonRequest):
    item_ids: list[int] | None = None
    expected_state_version: int
    expected_attempt_id: int | None = None
    expected_attempt_version: int | None = None
    expected_resolution_version: int | None = None
    confirm_remote_unknown: bool = False


class LoginBatchRefreshCredentialRequest(_ReasonRequest):
    code_url: str = Field(min_length=1)
    expected_item_version: int
    expected_binding_version: int | None = None
    replace_binding: bool = False


class LoginBatchCancelRequest(_ReasonRequest):
    expected_state_version: int


class LoginBatchCapabilityOut(BaseModel):
    mode: str
    max_lines: int
    worker_concurrency: int
    item_deadline_seconds: int
    code_wait_seconds: int
    poll_interval_seconds: int
    readiness: bool
    blockers: list[str]


class LoginBatchNotificationOut(ApiModel):
    id: int
    batch_id: int
    execution_generation: int
    resolution_version: int
    event_type: str
    channel: str
    summary: dict
    delivery_status: str
    acknowledged_at: datetime | None
    state_version: int
    created_at: datetime


class LoginBatchNotificationAckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int


class CodeSourceBindingRevealRequest(_ReasonRequest):
    expected_binding_version: int


class CodeSourceBindingRevealOut(BaseModel):
    account_id: int
    host: str
    uuid: str
    binding_version: int


__all__ = [name for name in globals() if name.startswith("LoginBatch") or name.startswith("CodeSource")]

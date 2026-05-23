from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, model_validator

from .api import ApiModel


class TargetRuntimeSummaryOut(ApiModel):
    id: str
    tenant_id: int
    target_id: int
    status: str
    open_issue_count: int
    failed_action_count: int
    affected_task_count: int
    latest_failure_at: datetime | None
    summary: dict[str, Any] = {}
    updated_at: datetime


class TaskRuntimeSummaryOut(ApiModel):
    id: str
    tenant_id: int
    task_id: str
    task_status: str
    target_id: int | None
    planned_count: int
    success_count: int
    failed_count: int
    pending_count: int
    oldest_pending_at: datetime | None
    latest_failure_type: str
    summary: dict[str, Any] = {}
    updated_at: datetime


class AccountRuntimeSummaryOut(ApiModel):
    id: str
    tenant_id: int
    account_id: int
    send_available: bool
    listen_available: bool
    join_available: bool
    comment_available: bool
    profile_available: bool
    code_read_available: bool
    remaining_capacity: int
    unavailable_reason: str
    next_retry_at: datetime | None
    failure_trend: dict[str, Any] = {}
    updated_at: datetime


class OperationIssueOut(ApiModel):
    id: str
    tenant_id: int
    target_id: int | None
    issue_type: str
    severity: str
    source_task_id: str
    representative_action_id: str
    affected_account_ids: list[int] = []
    failure_type: str
    failure_reason: str
    suggested_action: str
    status: str
    summary: dict[str, Any] = {}
    first_seen_at: datetime
    last_seen_at: datetime
    resolved_at: datetime | None
    updated_at: datetime


class OperationIssueFailureActionOut(ApiModel):
    id: str
    task_id: str
    task_name: str = ""
    task_type: str
    action_type: str
    account_id: int | None
    status: str
    failure_type: str
    failure_reason: str
    scheduled_at: datetime
    executed_at: datetime | None
    retry_count: int
    result: dict[str, Any] = {}


class OperationIssueDetailOut(BaseModel):
    issue: OperationIssueOut
    target: dict[str, Any] | None = None
    source_task: dict[str, Any] | None = None
    related_task_summary: TaskRuntimeSummaryOut | None = None
    affected_accounts: list[dict[str, Any]] = []
    recent_failed_actions: list[OperationIssueFailureActionOut] = []


class OperationCenterOverviewOut(BaseModel):
    tenant_id: int
    open_issue_count: int
    affected_target_count: int
    running_task_count: int
    failed_action_count: int
    affected_account_count: int
    latest_updated_at: datetime | None
    stale: bool = False


class OperationIssueStatusRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=255)

    @model_validator(mode="after")
    def normalize_reason(self) -> "OperationIssueStatusRequest":
        self.reason = self.reason.strip()
        if not self.reason:
            raise ValueError("处理原因不能为空")
        return self


__all__ = [
    "AccountRuntimeSummaryOut",
    "OperationCenterOverviewOut",
    "OperationIssueDetailOut",
    "OperationIssueFailureActionOut",
    "OperationIssueOut",
    "OperationIssueStatusRequest",
    "TargetRuntimeSummaryOut",
    "TaskRuntimeSummaryOut",
]

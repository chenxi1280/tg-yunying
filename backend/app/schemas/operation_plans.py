from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from .api import ApiModel


class OperationPlanCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str = ""
    target_type: str = "group"
    target_ids: list[int] = Field(default_factory=list)
    strategy_config: dict[str, Any] = Field(default_factory=dict)
    task_blueprints: list[dict[str, Any]] = Field(default_factory=list)


class OperationPlanUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = None
    target_type: str | None = None
    target_ids: list[int] | None = None
    status: str | None = None
    strategy_config: dict[str, Any] | None = None
    task_blueprints: list[dict[str, Any]] | None = None


class OperationPlanGenerateRequest(BaseModel):
    target_ids: list[int] | None = None
    auto_start: bool = False
    confirm_apply: bool = False
    reason: str = ""


class OperationPlanTargetOut(ApiModel):
    id: int
    tenant_id: int
    plan_id: int
    target_id: int
    status: str
    strategy_config: dict[str, Any] = {}
    created_at: datetime
    updated_at: datetime


class OperationPlanTaskLinkOut(ApiModel):
    id: int
    tenant_id: int
    plan_id: int
    target_id: int | None
    task_id: str
    relation: str
    status: str
    created_at: datetime


class OperationPlanGenerationRunOut(ApiModel):
    id: str
    tenant_id: int
    plan_id: int
    run_type: str
    status: str
    requested_by: str
    request_payload: dict[str, Any] = {}
    result_payload: dict[str, Any] = {}
    failure_detail: str
    created_at: datetime
    finished_at: datetime | None


class OperationPlanOut(ApiModel):
    id: int
    tenant_id: int
    name: str
    description: str
    target_type: str
    status: str
    strategy_config: dict[str, Any] = {}
    task_blueprints: list[dict[str, Any]] = []
    created_by: str
    updated_by: str
    created_at: datetime
    updated_at: datetime
    targets: list[OperationPlanTargetOut] = []
    task_links: list[OperationPlanTaskLinkOut] = []
    latest_run: OperationPlanGenerationRunOut | None = None


class OperationPlanPreviewOut(BaseModel):
    plan_id: int
    target_count: int
    estimated_task_count: int = 0
    estimated_target_count: int = 0
    account_capacity: dict[str, Any] = {}
    admission_actions: list[dict[str, Any]] = []
    target_previews: list[dict[str, Any]] = []
    planned_tasks: list[dict[str, Any]]
    blockers: list[str] = []
    warnings: list[str] = []
    run: OperationPlanGenerationRunOut


class OperationPlanGenerateOut(BaseModel):
    plan_id: int
    created_task_ids: list[str]
    linked_task_count: int
    run: OperationPlanGenerationRunOut


class OperationPlanApplyOut(BaseModel):
    plan_id: int
    created_task_ids: list[str] = []
    linked_task_count: int
    applied_task_ids: list[str] = []
    requires_confirmation: bool = True
    impact_preview: dict[str, Any] = {}
    run: OperationPlanGenerationRunOut


__all__ = [
    "OperationPlanApplyOut",
    "OperationPlanCreate",
    "OperationPlanGenerateOut",
    "OperationPlanGenerateRequest",
    "OperationPlanGenerationRunOut",
    "OperationPlanOut",
    "OperationPlanPreviewOut",
    "OperationPlanTargetOut",
    "OperationPlanTaskLinkOut",
    "OperationPlanUpdate",
]

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ListenerAccountOut(BaseModel):
    id: int
    display_name: str
    username: str | None = None
    status: str
    roles: list[str] = Field(default_factory=list)
    task_ids: list[str] = Field(default_factory=list)


class ListenerTaskOut(BaseModel):
    id: str
    name: str
    type: str
    status: str


class ListenerSnapshotOut(BaseModel):
    key: str
    object_type: Literal["channel", "group"]
    title: str
    peer_id: str
    status: str
    listener_account_count: int = 0
    subscriber_task_count: int = 0
    event_backlog_count: int = 0
    last_event_at: str | None = None
    last_error: str = ""
    task_ids: list[str] = Field(default_factory=list)
    listener_accounts: list[ListenerAccountOut] = Field(default_factory=list)
    subscriber_tasks: list[ListenerTaskOut] = Field(default_factory=list)


class ListenerSummaryOut(BaseModel):
    channel_count: int
    group_count: int
    subscriber_task_count: int
    items: list[ListenerSnapshotOut]


class RuleSummaryOut(BaseModel):
    key: str
    category: str
    name: str
    status: str
    detail: str
    version: str
    source: str = "system"
    metadata: dict[str, Any] = Field(default_factory=dict)


class RuleCenterSummaryOut(BaseModel):
    system_rule_count: int
    keyword_rule_count: int
    relay_task_rule_count: int
    items: list[RuleSummaryOut]


class RuleTestRequest(BaseModel):
    text: str = ""


class RuleTestHitOut(BaseModel):
    rule_id: int
    keyword: str
    match_type: str
    note: str = ""


class RuleTestOut(BaseModel):
    result: str
    hits: list[RuleTestHitOut] = Field(default_factory=list)


class RuleSetVersionCreate(BaseModel):
    filters: dict[str, Any] = Field(default_factory=dict)
    transforms: dict[str, Any] = Field(default_factory=dict)
    routing: dict[str, Any] = Field(default_factory=dict)
    account_strategy: dict[str, Any] = Field(default_factory=dict)
    rate_limits: dict[str, Any] = Field(default_factory=dict)
    retry_policy: dict[str, Any] = Field(default_factory=dict)


class RuleSetCreate(RuleSetVersionCreate):
    name: str = Field(min_length=1, max_length=160)
    description: str = ""


class RuleSetVersionOut(BaseModel):
    id: int
    tenant_id: int
    rule_set_id: int
    version: int
    status: str
    filters: dict[str, Any]
    transforms: dict[str, Any]
    routing: dict[str, Any]
    account_strategy: dict[str, Any]
    rate_limits: dict[str, Any]
    retry_policy: dict[str, Any]
    created_by: str
    published_by: str
    published_at: str | None = None
    created_at: str
    updated_at: str


class RuleSetOut(BaseModel):
    id: int
    tenant_id: int
    name: str
    description: str
    status: str
    active_version_id: int | None
    versions: list[RuleSetVersionOut] = Field(default_factory=list)
    created_at: str
    updated_at: str


class MetricBucketOut(BaseModel):
    key: str
    label: str
    value: int | float | str
    detail: str = ""
    status: str = ""


class OperationMetricsOut(BaseModel):
    accounts: list[MetricBucketOut] = Field(default_factory=list)
    targets: list[MetricBucketOut] = Field(default_factory=list)
    messages: list[MetricBucketOut] = Field(default_factory=list)
    channel_interactions: list[MetricBucketOut] = Field(default_factory=list)
    ai_activity: list[MetricBucketOut] = Field(default_factory=list)
    relay: list[MetricBucketOut] = Field(default_factory=list)
    archives: list[MetricBucketOut] = Field(default_factory=list)
    ai_usage: list[MetricBucketOut] = Field(default_factory=list)
    failures: list[MetricBucketOut] = Field(default_factory=list)


__all__ = [
    "ListenerAccountOut",
    "ListenerSnapshotOut",
    "ListenerSummaryOut",
    "ListenerTaskOut",
    "MetricBucketOut",
    "OperationMetricsOut",
    "RuleCenterSummaryOut",
    "RuleSummaryOut",
    "RuleSetCreate",
    "RuleSetOut",
    "RuleSetVersionCreate",
    "RuleSetVersionOut",
    "RuleTestHitOut",
    "RuleTestOut",
    "RuleTestRequest",
]

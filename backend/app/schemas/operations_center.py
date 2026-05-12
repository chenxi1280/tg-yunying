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


class ListenerEventOut(BaseModel):
    id: int
    event_type: str
    content: str
    account_id: int | None = None
    sender_name: str = ""
    occurred_at: str | None = None


class ListenerSnapshotOut(BaseModel):
    key: str
    object_type: Literal["channel", "group"]
    title: str
    peer_id: str
    status: str
    listener_account_count: int = 0
    subscriber_task_count: int = 0
    event_backlog_count: int = 0
    pending_distribution_count: int = 0
    dedup_event_count: int = 0
    subscription_event_types: list[str] = Field(default_factory=list)
    last_event_at: str | None = None
    last_error: str = ""
    backup_account: ListenerAccountOut | None = None
    switch_recommended: bool = False
    switch_reason: str = ""
    task_ids: list[str] = Field(default_factory=list)
    listener_accounts: list[ListenerAccountOut] = Field(default_factory=list)
    subscriber_tasks: list[ListenerTaskOut] = Field(default_factory=list)
    recent_events: list[ListenerEventOut] = Field(default_factory=list)


class ListenerSummaryOut(BaseModel):
    channel_count: int
    group_count: int
    subscriber_task_count: int
    items: list[ListenerSnapshotOut]


class ListenerSwitchRequest(BaseModel):
    backup_account_id: int | None = None


class RuleSummaryOut(BaseModel):
    key: str
    category: str
    name: str
    status: str
    detail: str
    version: str
    source: str = "system"
    metadata: dict[str, Any] = Field(default_factory=dict)


class RuleConflictOut(BaseModel):
    key: str
    level: str
    title: str
    detail: str
    related_ids: list[str] = Field(default_factory=list)


class RuleExecutionMetricOut(BaseModel):
    key: str
    rule_set_id: int | None = None
    rule_set_version_id: int | None = None
    rule_set_name: str = ""
    version: int | None = None
    task_count: int = 0
    action_count: int = 0
    success_count: int = 0
    failed_count: int = 0
    skipped_count: int = 0
    pending_count: int = 0
    last_used_at: str | None = None


class RuleDimensionMetricOut(BaseModel):
    key: str
    dimension: Literal["target", "account", "keyword"]
    name: str
    related_id: str = ""
    action_count: int = 0
    success_count: int = 0
    failed_count: int = 0
    skipped_count: int = 0
    pending_count: int = 0
    last_used_at: str | None = None


class RuleTrendMetricOut(BaseModel):
    date: str
    action_count: int = 0
    success_count: int = 0
    failed_count: int = 0
    skipped_count: int = 0
    pending_count: int = 0


class RuleConversionMetricOut(BaseModel):
    key: str
    rule_set_id: int | None = None
    rule_set_version_id: int | None = None
    rule_set_name: str = ""
    version: int | None = None
    current_action_count: int = 0
    current_success_count: int = 0
    current_success_rate: float = 0
    previous_action_count: int = 0
    previous_success_count: int = 0
    previous_success_rate: float = 0
    success_rate_delta: float = 0


class RuleCrossMetricOut(BaseModel):
    key: str
    rule_set_id: int | None = None
    rule_set_version_id: int | None = None
    rule_set_name: str = ""
    version: int | None = None
    target_group_id: int | None = None
    target_name: str = ""
    account_id: int | None = None
    account_name: str = ""
    action_count: int = 0
    success_count: int = 0
    failed_count: int = 0
    skipped_count: int = 0
    pending_count: int = 0
    success_rate: float = 0
    last_used_at: str | None = None


class RelayMaterialAttributionOut(BaseModel):
    key: str
    material_fingerprint: str
    sample_text: str = ""
    task_count: int = 0
    source_event_count: int = 0
    target_count: int = 0
    account_count: int = 0
    action_count: int = 0
    success_count: int = 0
    failed_count: int = 0
    skipped_count: int = 0
    pending_count: int = 0
    retry_count: int = 0
    success_rate: float = 0
    last_used_at: str | None = None


class RelayAttributionReportOut(BaseModel):
    total_materials: int = 0
    total_source_events: int = 0
    total_actions: int = 0
    rows: list[RelayMaterialAttributionOut] = Field(default_factory=list)


class RuleCenterSummaryOut(BaseModel):
    system_rule_count: int
    keyword_rule_count: int
    relay_task_rule_count: int
    items: list[RuleSummaryOut]
    conflicts: list[RuleConflictOut] = Field(default_factory=list)
    execution_metrics: list[RuleExecutionMetricOut] = Field(default_factory=list)
    target_metrics: list[RuleDimensionMetricOut] = Field(default_factory=list)
    account_metrics: list[RuleDimensionMetricOut] = Field(default_factory=list)
    keyword_metrics: list[RuleDimensionMetricOut] = Field(default_factory=list)
    trend_metrics: list[RuleTrendMetricOut] = Field(default_factory=list)
    conversion_metrics: list[RuleConversionMetricOut] = Field(default_factory=list)
    cross_metrics: list[RuleCrossMetricOut] = Field(default_factory=list)


class RuleTestRequest(BaseModel):
    text: str = ""
    rule_set_version_id: int | None = None
    source_group_id: int | None = None
    sender_id: str = ""
    message_type: str = "text"


class RuleTestHitOut(BaseModel):
    rule_id: int
    keyword: str
    match_type: str
    note: str = ""


class RuleTestRouteOut(BaseModel):
    group_id: int
    title: str
    status: str
    can_send_account_count: int = 0
    account_strategy: str = ""


class RuleTestOut(BaseModel):
    result: str
    hits: list[RuleTestHitOut] = Field(default_factory=list)
    should_block: bool = False
    block_reason: str = ""
    filter_passed: bool = True
    filter_reason: str = ""
    rule_set_version_id: int | None = None
    rule_set_name: str = ""
    transformed_text: str = ""
    target_summary: str = "按绑定任务/目标路由"
    target_routes: list[RuleTestRouteOut] = Field(default_factory=list)
    account_strategy: str = "按任务账号策略选择"
    rate_limit_summary: str = "执行时按账号冷却、小时/日上限校验"


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


class OperationMetricDetailOut(BaseModel):
    key: str
    title: str
    category: str
    status: str
    detail: str = ""
    related_id: str = ""
    occurred_at: str | None = None


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
    risk_control: list[MetricBucketOut] = Field(default_factory=list)
    account_details: list[OperationMetricDetailOut] = Field(default_factory=list)
    target_details: list[OperationMetricDetailOut] = Field(default_factory=list)
    task_details: list[OperationMetricDetailOut] = Field(default_factory=list)
    failure_details: list[OperationMetricDetailOut] = Field(default_factory=list)
    risk_details: list[OperationMetricDetailOut] = Field(default_factory=list)


__all__ = [
    "ListenerAccountOut",
    "ListenerEventOut",
    "ListenerSnapshotOut",
    "ListenerSummaryOut",
    "ListenerSwitchRequest",
    "ListenerTaskOut",
    "MetricBucketOut",
    "OperationMetricDetailOut",
    "OperationMetricsOut",
    "RuleCenterSummaryOut",
    "RuleConflictOut",
    "RelayAttributionReportOut",
    "RelayMaterialAttributionOut",
    "RuleConversionMetricOut",
    "RuleCrossMetricOut",
    "RuleDimensionMetricOut",
    "RuleExecutionMetricOut",
    "RuleSummaryOut",
    "RuleSetCreate",
    "RuleSetOut",
    "RuleSetVersionCreate",
    "RuleSetVersionOut",
    "RuleTestHitOut",
    "RuleTestOut",
    "RuleTestRequest",
    "RuleTestRouteOut",
    "RuleTrendMetricOut",
]

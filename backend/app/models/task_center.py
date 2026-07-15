from __future__ import annotations

from datetime import date, datetime
from uuid import uuid4

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

from .enums import now


def new_uuid() -> str:
    return str(uuid4())


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), default=1)
    name: Mapped[str] = mapped_column(String(200))
    type: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(20), default="draft")
    priority: Mapped[int] = mapped_column(Integer, default=3)
    timezone: Mapped[str] = mapped_column(String(50), default="Asia/Shanghai")
    scheduled_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    scheduled_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    max_duration_hours: Mapped[int | None] = mapped_column(Integer, nullable=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    account_config: Mapped[dict] = mapped_column(JSON, default=dict)
    pacing_config: Mapped[dict] = mapped_column(JSON, default=dict)
    failure_policy: Mapped[dict] = mapped_column(JSON, default=dict)
    type_config: Mapped[dict] = mapped_column(JSON, default=dict)
    stats: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_by: Mapped[str] = mapped_column(String(100), default="")
    delete_reason: Mapped[str] = mapped_column(String(255), default="")


class Action(Base):
    __tablename__ = "actions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "action_dedupe_key", name="uq_actions_action_dedupe_key"),
        Index("ix_actions_due_claim", "status", "scheduled_at", "created_at"),
        Index("ix_actions_claim_expiry", "status", "claim_expires_at"),
        Index("ix_actions_lease_recovery", "lease_owner", "lease_expires_at"),
        Index("ix_actions_task_status", "task_id", "status", "executed_at"),
        Index(
            "ix_actions_task_stats_reconcile",
            "tenant_id",
            "task_id",
            "status",
            "action_type",
            "account_id",
            "executed_at",
        ),
        Index(
            "ix_actions_executing_recovery",
            "scheduled_at",
            "lease_expires_at",
            "task_id",
            "id",
            sqlite_where=text("status = 'executing'"),
            postgresql_where=text("status = 'executing'"),
        ),
        Index("ix_actions_executed_at_status", "executed_at", "status"),
        Index("ix_actions_created_at", "created_at"),
        Index("ix_actions_account_occupied_at", "tenant_id", "account_id", "status", text("(coalesce(executed_at, scheduled_at))")),
        Index("ix_actions_task_schedule_page", "tenant_id", "task_id", "scheduled_at", "created_at"),
        Index("ix_actions_task_status_schedule_page", "tenant_id", "task_id", "status", "scheduled_at", "created_at"),
        Index("ix_actions_task_type_schedule_page", "tenant_id", "task_id", "action_type", "scheduled_at", "created_at"),
        Index("ix_actions_task_type_executed_at", "tenant_id", "task_id", "action_type", "executed_at"),
        Index(
            "uq_actions_executing_account",
            "account_id",
            unique=True,
            sqlite_where=text("status = 'executing' AND account_id IS NOT NULL"),
            postgresql_where=text("status = 'executing' AND account_id IS NOT NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), default=1)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"))
    task_type: Mapped[str] = mapped_column(String(30))
    action_type: Mapped[str] = mapped_column(String(30))
    account_id: Mapped[int | None] = mapped_column(ForeignKey("tg_accounts.id"), nullable=True)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    lease_owner: Mapped[str] = mapped_column(String(120), default="")
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claim_owner: Mapped[str] = mapped_column(String(120), default="")
    claim_token: Mapped[str] = mapped_column(String(80), default="")
    claim_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    plan_batch_key: Mapped[str | None] = mapped_column(String(160), nullable=True)
    action_dedupe_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ExecutionAttempt(Base):
    __tablename__ = "execution_attempts"
    __table_args__ = (
        UniqueConstraint("action_id", "attempt_no", name="uq_execution_attempts_action_attempt"),
        Index("ix_execution_attempts_unfinished", "status", "gateway_call_started_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), default=1)
    action_id: Mapped[str] = mapped_column(ForeignKey("actions.id"))
    worker_id: Mapped[str] = mapped_column(String(160), default="")
    account_id: Mapped[int | None] = mapped_column(ForeignKey("tg_accounts.id"), nullable=True)
    attempt_no: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(40), default="before_call")
    before_call_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    gateway_call_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    after_call_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    remote_message_id: Mapped[str] = mapped_column(String(160), default="")
    failure_type: Mapped[str] = mapped_column(String(80), default="")
    failure_detail: Mapped[str] = mapped_column(Text, default="")
    result_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class DailyRuntimeStat(Base):
    __tablename__ = "daily_runtime_stats"
    __table_args__ = (
        UniqueConstraint("stat_date", "dimension_type", "dimension_id", "metric_name", name="uq_daily_runtime_stats_metric"),
        Index("ix_daily_runtime_stats_dimension", "dimension_type", "dimension_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    stat_date: Mapped[date] = mapped_column(Date)
    dimension_type: Mapped[str] = mapped_column(String(40))
    dimension_id: Mapped[str] = mapped_column(String(120), default="")
    metric_name: Mapped[str] = mapped_column(String(80))
    metric_value: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class RuntimeCleanupAudit(Base):
    __tablename__ = "runtime_cleanup_audits"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    cleanup_date: Mapped[date] = mapped_column(Date)
    status_counts: Mapped[dict] = mapped_column(JSON, default=dict)
    deleted_counts: Mapped[dict] = mapped_column(JSON, default=dict)
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class RuntimeMetricSnapshot(Base):
    __tablename__ = "runtime_metric_snapshots"
    __table_args__ = (
        Index("ix_runtime_metric_snapshots_captured", "captured_at"),
        Index("ix_runtime_metric_snapshots_metric_dimension", "metric_name", "dimension_type", "dimension_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    metric_name: Mapped[str] = mapped_column(String(120))
    dimension_type: Mapped[str] = mapped_column(String(40), default="global")
    dimension_id: Mapped[str] = mapped_column(String(120), default="all")
    metric_value: Mapped[int] = mapped_column(Integer, default=0)
    tags: Mapped[dict] = mapped_column(JSON, default=dict)


class BotProtocolSample(Base):
    __tablename__ = "bot_protocol_samples"
    __table_args__ = (
        Index("ix_bot_protocol_samples_active", "tenant_id", "bot_username", "sample_type", "is_active"),
        Index("ix_bot_protocol_samples_captured", "tenant_id", "bot_username", "captured_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), default=1)
    bot_username: Mapped[str] = mapped_column(String(80), default="")
    sample_type: Mapped[str] = mapped_column(String(60), default="search_results")
    sample_purpose: Mapped[str] = mapped_column(String(40), default="search_join")
    sample_hash: Mapped[str] = mapped_column(String(120), default="")
    schema_version: Mapped[str] = mapped_column(String(40), default="v1")
    structure_json: Mapped[dict] = mapped_column(JSON, default=dict)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    pii_scrubbed: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class SearchJoinRankObservation(Base):
    __tablename__ = "search_join_rank_observations"
    __table_args__ = (
        Index("ix_search_join_rank_task_time", "tenant_id", "task_id", "observed_at"),
        Index("ix_search_join_rank_keyword", "tenant_id", "keyword_hash", "observed_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), default=1)
    task_id: Mapped[str] = mapped_column(String(36))
    bot_username: Mapped[str] = mapped_column(String(80), default="")
    keyword_hash: Mapped[str] = mapped_column(String(64), default="")
    target_group_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    observed_position: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_results: Mapped[int | None] = mapped_column(Integer, nullable=True)
    observed_region: Mapped[str] = mapped_column(String(80), default="")
    observation_source: Mapped[str] = mapped_column(String(80), default="")
    paid_keyword_ad_status: Mapped[str] = mapped_column(String(40), default="unknown")
    jisou_ecosystem_status: Mapped[str] = mapped_column(String(40), default="unknown")
    target_relevance_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    target_content_health: Mapped[str] = mapped_column(String(40), default="unknown")
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class SearchJoinLinkedTaskDispatch(Base):
    __tablename__ = "search_join_linked_task_dispatches"
    __table_args__ = (
        Index("ix_search_join_linked_source", "tenant_id", "source_task_id", "created_at"),
        Index("ix_search_join_linked_status", "tenant_id", "status", "activation_not_before"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), default=1)
    search_join_action_id: Mapped[str] = mapped_column(String(36), default="")
    source_task_id: Mapped[str] = mapped_column(String(36), default="")
    linked_task_id: Mapped[str] = mapped_column(String(36), default="")
    account_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    target_group_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    link_type: Mapped[str] = mapped_column(String(40), default="")
    status: Mapped[str] = mapped_column(String(40), default="linked_task_ready_pending")
    block_reason: Mapped[str] = mapped_column(String(120), default="")
    can_send_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    activation_not_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ready_pool_item_id: Mapped[str] = mapped_column(String(80), default="")
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class TaskMembershipAdmissionItem(Base):
    __tablename__ = "task_membership_admission_items"
    __table_args__ = (
        UniqueConstraint("task_id", "account_id", name="uq_membership_admission_task_account"),
        Index("ix_membership_admission_task_phase", "task_id", "phase"),
        Index("ix_membership_admission_manual", "task_id", "manual_required"),
        Index("ix_membership_admission_membership_action", "membership_action_id"),
        Index("ix_membership_admission_test_message_action", "test_message_action_id"),
        Index("ix_membership_admission_delete_action", "delete_action_id"),
        Index("ix_membership_admission_rescue_action", "rescue_action_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), default=1)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"))
    account_id: Mapped[int] = mapped_column(ForeignKey("tg_accounts.id"))
    target_id: Mapped[int] = mapped_column(ForeignKey("operation_targets.id"))
    phase: Mapped[str] = mapped_column(String(40), default="pending")
    membership_action_id: Mapped[str | None] = mapped_column(ForeignKey("actions.id"), nullable=True)
    test_message_action_id: Mapped[str | None] = mapped_column(ForeignKey("actions.id"), nullable=True)
    delete_action_id: Mapped[str | None] = mapped_column(ForeignKey("actions.id"), nullable=True)
    test_message_text: Mapped[str] = mapped_column(Text, default="")
    test_message_id: Mapped[str] = mapped_column(String(160), default="")
    delete_after_send: Mapped[bool] = mapped_column(Boolean, default=False)
    delete_status: Mapped[str] = mapped_column(String(40), default="")
    failure_type: Mapped[str] = mapped_column(String(80), default="")
    failure_detail: Mapped[str] = mapped_column(Text, default="")
    manual_required: Mapped[bool] = mapped_column(Boolean, default=False)
    permission_failure_count: Mapped[int] = mapped_column(Integer, default=0)
    rescue_action_id: Mapped[str | None] = mapped_column(ForeignKey("actions.id"), nullable=True)
    rescue_status: Mapped[str] = mapped_column(String(40), default="")
    rescue_failure_detail: Mapped[str] = mapped_column(Text, default="")
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class ListenerSourceState(Base):
    __tablename__ = "listener_source_state"
    __table_args__ = (
        UniqueConstraint("tenant_id", "source_type", "source_peer_id", "account_id", name="uq_listener_source_state_source"),
        Index("ix_listener_source_state_claim", "shard_key", "lease_expires_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), default=1)
    source_type: Mapped[str] = mapped_column(String(40), default="group")
    source_peer_id: Mapped[str] = mapped_column(String(160), default="")
    account_id: Mapped[int | None] = mapped_column(ForeignKey("tg_accounts.id"), nullable=True)
    shard_key: Mapped[str] = mapped_column(String(80), default="")
    lease_owner: Mapped[str] = mapped_column(String(160), default="")
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_remote_message_id: Mapped[str] = mapped_column(String(160), default="")
    last_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    backfill_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    collect_window_seconds: Mapped[int] = mapped_column(Integer, default=30)
    last_error: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class TgAccountOnlineState(Base):
    __tablename__ = "tg_account_online_state"
    __table_args__ = (
        UniqueConstraint("tenant_id", "account_id", name="uq_tg_account_online_state_account"),
        Index("ix_tg_account_online_state_status", "tenant_id", "desired_online", "online_status"),
        Index("ix_tg_account_online_state_next_probe", "tenant_id", "next_probe_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), default=1)
    account_id: Mapped[int] = mapped_column(ForeignKey("tg_accounts.id"))
    desired_online: Mapped[bool] = mapped_column(Boolean, default=False)
    desired_sources: Mapped[list] = mapped_column(JSON, default=list)
    online_status: Mapped[str] = mapped_column(String(40), default="offline")
    session_kind: Mapped[str] = mapped_column(String(40), default="")
    session_id: Mapped[str] = mapped_column(String(80), default="")
    proxy_id: Mapped[int | None] = mapped_column(ForeignKey("account_proxies.id"), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_probe_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_keepalive_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    stale_after_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failure_type: Mapped[str] = mapped_column(String(80), default="")
    failure_detail: Mapped[str] = mapped_column(Text, default="")
    recovery_status: Mapped[str] = mapped_column(String(40), default="")
    next_probe_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    active_task_count: Mapped[int] = mapped_column(Integer, default=0)
    reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class AiGroupMessageMemory(Base):
    __tablename__ = "ai_group_message_memory"
    __table_args__ = (
        Index(
            "uq_ai_group_message_memory_reservation_key",
            "reservation_key",
            unique=True,
            sqlite_where=text("reservation_key <> ''"),
            postgresql_where=text("reservation_key <> ''"),
        ),
        Index("ix_ai_group_message_memory_dedupe", "tenant_id", "group_id", "text_fingerprint", "status", "planned_at"),
        Index("ix_ai_group_message_memory_expiry", "status", "expires_at"),
        Index("ix_ai_group_message_memory_task", "tenant_id", "task_id", "planned_at"),
        Index("ix_ai_group_message_memory_action_id", "action_id"),
        Index(
            "ix_ai_group_message_memory_tenant_status_planned",
            "tenant_id",
            "status",
            text("planned_at DESC"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), default=1)
    group_id: Mapped[int] = mapped_column(Integer)
    task_id: Mapped[str] = mapped_column(String(36), default="")
    action_id: Mapped[str] = mapped_column(String(36), default="")
    account_id: Mapped[int | None] = mapped_column(ForeignKey("tg_accounts.id"), nullable=True)
    topic_direction: Mapped[str] = mapped_column(Text, default="")
    teacher_target: Mapped[str] = mapped_column(Text, default="")
    raw_text: Mapped[str] = mapped_column(Text, default="")
    normalized_text: Mapped[str] = mapped_column(Text, default="")
    text_fingerprint: Mapped[str] = mapped_column(String(64), default="")
    semantic_cluster: Mapped[str] = mapped_column(String(128), default="")
    template_shell_key: Mapped[str] = mapped_column(String(128), default="")
    reservation_key: Mapped[str] = mapped_column(String(160), default="")
    status: Mapped[str] = mapped_column(String(40), default="reserved")
    planned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duplicate_window: Mapped[str] = mapped_column(String(40), default="")
    duplicate_reference_id: Mapped[str] = mapped_column(String(36), default="")
    quality_decision: Mapped[str] = mapped_column(String(80), default="")
    profile_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    profile_match_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    profile_match_reason: Mapped[str] = mapped_column(Text, default="")
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class AiAccountVoiceProfile(Base):
    __tablename__ = "ai_account_voice_profiles"
    __table_args__ = (
        UniqueConstraint("tenant_id", "account_id", "version", name="uq_ai_account_voice_profiles_version"),
        Index("ix_ai_account_voice_profiles_account_status", "tenant_id", "account_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), default=1)
    account_id: Mapped[int] = mapped_column(ForeignKey("tg_accounts.id"))
    version: Mapped[int] = mapped_column(Integer, default=1)
    mask_name: Mapped[str] = mapped_column(String(80), default="")
    audience_archetype: Mapped[str] = mapped_column(String(120), default="")
    identity_frame: Mapped[str] = mapped_column(String(160), default="")
    preference_tags: Mapped[list] = mapped_column(JSON, default=list)
    age_band: Mapped[str] = mapped_column(String(40), default="")
    persona_experiences: Mapped[list] = mapped_column(JSON, default=list)
    consumption_experiences: Mapped[list] = mapped_column(JSON, default=list)
    sentence_length: Mapped[str] = mapped_column(String(80), default="")
    interaction_habits: Mapped[list] = mapped_column(JSON, default=list)
    tone_strength: Mapped[str] = mapped_column(String(80), default="")
    lexical_preferences: Mapped[list] = mapped_column(JSON, default=list)
    emoji_policy: Mapped[str] = mapped_column(String(80), default="")
    forbidden_expressions: Mapped[list] = mapped_column(JSON, default=list)
    short_prompt_summary: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(40), default="ai_batch")
    status: Mapped[str] = mapped_column(String(40), default="active")
    similarity_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    quality_status: Mapped[str] = mapped_column(String(40), default="active")
    last_rebuilt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_by: Mapped[str] = mapped_column(String(120), default="system")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class AiAccountGroupStanceMemory(Base):
    __tablename__ = "ai_account_group_stance_memory"
    __table_args__ = (
        UniqueConstraint("tenant_id", "group_id", "account_id", name="uq_ai_account_group_stance"),
        Index("ix_ai_account_group_stance_group", "tenant_id", "group_id", "account_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), default=1)
    group_id: Mapped[int] = mapped_column(Integer)
    account_id: Mapped[int] = mapped_column(ForeignKey("tg_accounts.id"))
    topic_direction: Mapped[str] = mapped_column(Text, default="")
    teacher_target: Mapped[str] = mapped_column(Text, default="")
    stance: Mapped[str] = mapped_column(String(120), default="")
    last_act_type: Mapped[str] = mapped_column(String(60), default="")
    last_semantic_cluster: Mapped[str] = mapped_column(String(128), default="")
    last_message_id: Mapped[str] = mapped_column(String(160), default="")
    last_spoken_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    window_start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    window_end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class ReviewQueue(Base):
    __tablename__ = "review_queue"
    __table_args__ = (Index("ix_review_queue_action_id", "action_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), default=1)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"))
    action_id: Mapped[str] = mapped_column(ForeignKey("actions.id"))
    content_preview: Mapped[str] = mapped_column(Text, default="")
    source_info: Mapped[str] = mapped_column(String(500), default="")
    status: Mapped[str] = mapped_column(String(20), default="pending")
    reviewed_by: Mapped[str] = mapped_column(String(100), default="")
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reject_reason: Mapped[str] = mapped_column(Text, default="")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class MessageFingerprint(Base):
    __tablename__ = "message_fingerprints"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), default=1)
    source_group_id: Mapped[str] = mapped_column(String(100), default="")
    fingerprint: Mapped[str] = mapped_column(String(64))
    semantic_hash: Mapped[str] = mapped_column(String(128), default="")
    original_text: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class SourceMediaAsset(Base):
    __tablename__ = "source_media_assets"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "source_group_id",
            "source_message_id",
            "source_media_group_id",
            "media_group_index",
            name="uq_source_media_assets_dedupe",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), default=1)
    source_group_id: Mapped[int | None] = mapped_column(ForeignKey("tg_groups.id"), nullable=True)
    listener_account_id: Mapped[int | None] = mapped_column(ForeignKey("tg_accounts.id"), nullable=True)
    source_peer_id: Mapped[str] = mapped_column(String(160), default="")
    source_message_id: Mapped[str] = mapped_column(String(160), default="")
    source_media_group_id: Mapped[str] = mapped_column(String(160), default="")
    media_group_index: Mapped[int] = mapped_column(Integer, default=0)
    media_group_total: Mapped[int] = mapped_column(Integer, default=1)
    album_caption_policy: Mapped[str] = mapped_column(String(40), default="per_item")
    media_type: Mapped[str] = mapped_column(String(40), default="photo")
    caption: Mapped[str] = mapped_column(Text, default="")
    media_fingerprint: Mapped[str] = mapped_column(String(128), default="")
    cache_status: Mapped[str] = mapped_column(String(40), default="pending_cache")
    cache_version: Mapped[int] = mapped_column(Integer, default=1)
    cache_peer_id: Mapped[str] = mapped_column(String(160), default="")
    cache_message_id: Mapped[str] = mapped_column(String(160), default="")
    failure_reason: Mapped[str] = mapped_column(Text, default="")
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_cached_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class TargetLearningSample(Base):
    __tablename__ = "target_learning_samples"
    __table_args__ = (
        UniqueConstraint("tenant_id", "target_id", "profile_scene", "source_message_id", name="uq_target_learning_samples_message"),
        Index("ix_target_learning_samples_target_status", "target_id", "profile_scene", "learning_status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), default=1)
    target_id: Mapped[int] = mapped_column(ForeignKey("operation_targets.id"))
    source_message_id: Mapped[str] = mapped_column(String(160), default="")
    source_scene: Mapped[str] = mapped_column(String(60), default="listener")
    profile_scene: Mapped[str] = mapped_column(String(60), default="group_chat")
    sender_peer_id: Mapped[str] = mapped_column(String(160), default="")
    sender_username: Mapped[str] = mapped_column(String(160), default="")
    sender_name: Mapped[str] = mapped_column(String(180), default="")
    is_bot: Mapped[bool] = mapped_column(Boolean, default=False)
    is_managed_account: Mapped[bool] = mapped_column(Boolean, default=False)
    message_type: Mapped[str] = mapped_column(String(40), default="text")
    text: Mapped[str] = mapped_column(Text, default="")
    caption: Mapped[str] = mapped_column(Text, default="")
    learning_status: Mapped[str] = mapped_column(String(40), default="candidate")
    reject_reason: Mapped[str] = mapped_column(String(160), default="")
    downweight_reason: Mapped[str] = mapped_column(String(160), default="")
    quality_score: Mapped[int] = mapped_column(Integer, default=100)
    observed_reply_count: Mapped[int] = mapped_column(Integer, default=0)
    applied_profile_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class TargetLearningProfile(Base):
    __tablename__ = "target_learning_profiles"
    __table_args__ = (
        UniqueConstraint("tenant_id", "target_id", "profile_scene", name="uq_target_learning_profiles_scene"),
        Index("ix_target_learning_profiles_target", "target_id", "profile_scene"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), default=1)
    target_id: Mapped[int] = mapped_column(ForeignKey("operation_targets.id"))
    profile_scene: Mapped[str] = mapped_column(String(60), default="group_chat")
    learning_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    style_summary: Mapped[str] = mapped_column(Text, default="")
    topic_weights: Mapped[dict] = mapped_column(JSON, default=dict)
    phrase_patterns: Mapped[list] = mapped_column(JSON, default=list)
    reply_patterns: Mapped[list] = mapped_column(JSON, default=list)
    comment_patterns: Mapped[list] = mapped_column(JSON, default=list)
    slang_terms: Mapped[list] = mapped_column(JSON, default=list)
    forbidden_learning: Mapped[list] = mapped_column(JSON, default=list)
    active_windows: Mapped[list] = mapped_column(JSON, default=list)
    profile_version: Mapped[int] = mapped_column(Integer, default=0)
    source_sample_count: Mapped[int] = mapped_column(Integer, default=0)
    disabled_reason: Mapped[str] = mapped_column(Text, default="")
    last_rebuilt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class TargetLearningProfileVersion(Base):
    __tablename__ = "target_learning_profile_versions"
    __table_args__ = (Index("ix_target_learning_profile_versions_profile", "profile_id", "profile_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), default=1)
    profile_id: Mapped[str] = mapped_column(ForeignKey("target_learning_profiles.id"))
    profile_version: Mapped[int] = mapped_column(Integer, default=1)
    source_sample_count: Mapped[int] = mapped_column(Integer, default=0)
    sample_window_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sample_window_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    summary_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    quality_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    created_by: Mapped[str] = mapped_column(String(120), default="system")


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    worker_id: Mapped[str] = mapped_column(String(160), unique=True)
    process_type: Mapped[str] = mapped_column(String(60), default="task_center")
    hostname: Mapped[str] = mapped_column(String(120), default="")
    pid: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(30), default="active")
    heartbeat_metadata: Mapped[dict] = mapped_column(JSON, default=dict)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


__all__ = [
    "Action",
    "AiAccountGroupStanceMemory",
    "AiAccountVoiceProfile",
    "AiGroupMessageMemory",
    "DailyRuntimeStat",
    "ExecutionAttempt",
    "ListenerSourceState",
    "MessageFingerprint",
    "ReviewQueue",
    "RuntimeMetricSnapshot",
    "RuntimeCleanupAudit",
    "BotProtocolSample",
    "SearchJoinLinkedTaskDispatch",
    "SearchJoinRankObservation",
    "SourceMediaAsset",
    "Task",
    "TaskMembershipAdmissionItem",
    "TgAccountOnlineState",
    "TargetLearningProfile",
    "TargetLearningProfileVersion",
    "TargetLearningSample",
    "WorkerHeartbeat",
    "new_uuid",
]

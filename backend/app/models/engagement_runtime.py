from __future__ import annotations

from datetime import date, datetime
from uuid import uuid4

from sqlalchemy import Date, DateTime, ForeignKey, Index, Integer, JSON, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

from .enums import now
from .task_center import ExecutionAttempt as _ExecutionAttempt


# Shared-resource reads include issued and unknown legacy calls, not only success.
Index("ix_execution_attempts_account_usage", _ExecutionAttempt.tenant_id,
    _ExecutionAttempt.account_id, _ExecutionAttempt.gateway_call_started_at,
    postgresql_where=text("gateway_call_started_at IS NOT NULL OR status IN ('success','result_unknown')"),
    sqlite_where=text("gateway_call_started_at IS NOT NULL OR status IN ('success','result_unknown')"))


def _new_uuid() -> str:
    return str(uuid4())


class ExecutionResiliencePolicyRevision(Base):
    __tablename__ = "execution_resilience_policy_revisions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "revision", name="uq_execution_resilience_policy_revision"),
        Index(
            "uq_execution_resilience_policy_active",
            "tenant_id",
            unique=True,
            postgresql_where=text("state = 'active'"),
            sqlite_where=text("state = 'active'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    telegram_connect_timeout_seconds: Mapped[int] = mapped_column(Integer, default=5)
    telegram_gateway_timeout_seconds: Mapped[int] = mapped_column(Integer, default=10)
    llm_invocation_timeout_seconds: Mapped[int] = mapped_column(Integer, default=15)
    proxy_route_inflight_limit: Mapped[int] = mapped_column(Integer, default=2)
    proxy_egress_inflight_limit: Mapped[int] = mapped_column(Integer, default=2)
    circuit_window_seconds: Mapped[int] = mapped_column(Integer, default=300)
    circuit_failure_threshold: Mapped[int] = mapped_column(Integer, default=2)
    circuit_open_seconds: Mapped[int] = mapped_column(Integer, default=900)
    task_contention_base_cap_bps: Mapped[int] = mapped_column(Integer, default=3000)
    state: Mapped[str] = mapped_column(String(24), default="active")
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AccountPoolConcurrencyPolicyRevision(Base):
    __tablename__ = "account_pool_concurrency_policy_revisions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "account_pool_id", "revision", name="uq_pool_concurrency_policy_revision"),
        Index(
            "uq_pool_concurrency_policy_active",
            "tenant_id",
            "account_pool_id",
            unique=True,
            postgresql_where=text("state = 'active'"),
            sqlite_where=text("state = 'active'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    account_pool_id: Mapped[int] = mapped_column(ForeignKey("account_pools.id", ondelete="CASCADE"))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    hard_remote_inflight_limit: Mapped[int] = mapped_column(Integer)
    workload_policy: Mapped[dict] = mapped_column(JSON, default=dict)
    state: Mapped[str] = mapped_column(String(24), default="active")
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AccountPoolConcurrencyLease(Base):
    __tablename__ = "account_pool_concurrency_leases"
    __table_args__ = (
        UniqueConstraint("account_pool_id", "invocation_identity", name="uq_pool_concurrency_invocation"),
        Index("ix_pool_concurrency_active", "account_pool_id", "state", "acquired_at"),
        Index("ix_pool_concurrency_task_share", "task_id", "account_pool_id", "state"),
        Index("ix_pool_concurrency_proxy_route", "tenant_id", "proxy_route_key", "state"),
        Index("ix_pool_concurrency_proxy_egress", "tenant_id", "proxy_egress_key", "state"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    policy_revision_id: Mapped[str] = mapped_column(ForeignKey("account_pool_concurrency_policy_revisions.id"))
    account_pool_id: Mapped[int] = mapped_column(ForeignKey("account_pools.id", ondelete="CASCADE"))
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    account_id: Mapped[int] = mapped_column(ForeignKey("tg_accounts.id", ondelete="CASCADE"))
    action_id: Mapped[str] = mapped_column(ForeignKey("actions.id", ondelete="CASCADE"))
    attempt_id: Mapped[str] = mapped_column(ForeignKey("execution_attempts.id", ondelete="CASCADE"))
    invocation_identity: Mapped[str] = mapped_column(String(160))
    task_group_share_limit: Mapped[int] = mapped_column(Integer)
    proxy_route_key: Mapped[str] = mapped_column(String(80), default="")
    proxy_egress_key: Mapped[str] = mapped_column(String(120), default="")
    fencing_token: Mapped[str] = mapped_column(String(80), default=_new_uuid)
    state: Mapped[str] = mapped_column(String(32), default="reserved")
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    release_reason: Mapped[str] = mapped_column(String(80), default="")


class AccountBehaviorBudgetPolicyRevision(Base):
    __tablename__ = "account_behavior_budget_policy_revisions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "account_class", "revision", name="uq_behavior_budget_policy_revision"),
        Index(
            "uq_behavior_budget_policy_active",
            "tenant_id",
            "account_class",
            unique=True,
            postgresql_where=text("state = 'active'"),
            sqlite_where=text("state = 'active'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    account_class: Mapped[str] = mapped_column(String(40), default="normal")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    action_budgets: Mapped[dict] = mapped_column(JSON, default=dict)
    session_budget: Mapped[dict] = mapped_column(JSON, default=dict)
    wake_budget: Mapped[int] = mapped_column(Integer, default=0)
    pair_gap_policy: Mapped[dict] = mapped_column(JSON, default=dict)
    state: Mapped[str] = mapped_column(String(24), default="active")
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AccountBehaviorBudgetLedger(Base):
    __tablename__ = "account_behavior_budget_ledgers"
    __table_args__ = (
        UniqueConstraint("tenant_id", "account_id", "task_day", name="uq_behavior_budget_account_day"),
        Index("ix_behavior_budget_task_day", "tenant_id", "task_day", "account_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    account_id: Mapped[int] = mapped_column(ForeignKey("tg_accounts.id", ondelete="CASCADE"))
    task_day: Mapped[date] = mapped_column(Date)
    policy_revision_id: Mapped[str] = mapped_column(ForeignKey("account_behavior_budget_policy_revisions.id"))
    action_budgets: Mapped[dict] = mapped_column(JSON, default=dict)
    counters: Mapped[dict] = mapped_column(JSON, default=dict)
    session_counters: Mapped[dict] = mapped_column(JSON, default=dict)
    wake_count: Mapped[int] = mapped_column(Integer, default=0)
    rest_debt: Mapped[int] = mapped_column(Integer, default=0)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class AccountBehaviorBudgetReservation(Base):
    __tablename__ = "account_behavior_budget_reservations"
    __table_args__ = (
        UniqueConstraint("attempt_id", name="uq_behavior_budget_attempt"),
        Index("ix_behavior_budget_reservation_state", "ledger_id", "action_class", "state"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    ledger_id: Mapped[str] = mapped_column(ForeignKey("account_behavior_budget_ledgers.id", ondelete="CASCADE"))
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    action_id: Mapped[str] = mapped_column(ForeignKey("actions.id", ondelete="CASCADE"))
    attempt_id: Mapped[str] = mapped_column(ForeignKey("execution_attempts.id", ondelete="CASCADE"))
    action_class: Mapped[str] = mapped_column(String(40))
    amount: Mapped[int] = mapped_column(Integer, default=1)
    state: Mapped[str] = mapped_column(String(32), default="reserved")
    reserved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RemoteInvocationFence(Base):
    __tablename__ = "remote_invocation_fences"
    __table_args__ = (
        UniqueConstraint("invocation_identity", name="uq_remote_invocation_fence_identity"),
        Index("ix_remote_invocation_fence_active", "state", "invocation_kind", "started_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    action_id: Mapped[str] = mapped_column(ForeignKey("actions.id", ondelete="CASCADE"))
    attempt_id: Mapped[str] = mapped_column(ForeignKey("execution_attempts.id", ondelete="CASCADE"))
    invocation_identity: Mapped[str] = mapped_column(String(160))
    invocation_kind: Mapped[str] = mapped_column(String(40))
    domain_keys: Mapped[dict] = mapped_column(JSON, default=dict)
    resilience_policy_revision_id: Mapped[str] = mapped_column(ForeignKey("execution_resilience_policy_revisions.id"))
    state: Mapped[str] = mapped_column(String(32), default="reserved")
    runner_generation: Mapped[int] = mapped_column(Integer, default=1)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancellation_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    transport_termination_state: Mapped[str] = mapped_column(
        String(40), default="not_requested"
    )
    transport_terminated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    business_outcome_state: Mapped[str] = mapped_column(String(32), default="not_called")
    terminal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ExecutionCircuitState(Base):
    __tablename__ = "execution_circuit_states"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "domain_kind",
            "domain_key",
            name="uq_execution_circuit_domain",
        ),
        Index(
            "ix_execution_circuit_open",
            "tenant_id",
            "state",
            "opened_until",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    resilience_policy_revision_id: Mapped[str] = mapped_column(
        ForeignKey("execution_resilience_policy_revisions.id")
    )
    domain_kind: Mapped[str] = mapped_column(String(32))
    domain_key: Mapped[str] = mapped_column(String(120))
    state: Mapped[str] = mapped_column(String(24), default="closed")
    failure_times: Mapped[list[str]] = mapped_column(JSON, default=list)
    opened_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    probe_attempt_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    probe_lease_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_failure_code: Mapped[str] = mapped_column(String(80), default="")
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class HealthProbeAttempt(Base):
    __tablename__ = "health_probe_attempts"
    __table_args__ = (
        UniqueConstraint(
            "circuit_state_id",
            "probe_revision",
            name="uq_health_probe_circuit_revision",
        ),
        Index(
            "uq_health_probe_circuit_active",
            "circuit_state_id",
            unique=True,
            postgresql_where=text("state IN ('claimed', 'running')"),
            sqlite_where=text("state IN ('claimed', 'running')"),
        ),
        Index("ix_health_probe_due", "state", "deadline_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE")
    )
    circuit_state_id: Mapped[str] = mapped_column(
        ForeignKey("execution_circuit_states.id", ondelete="CASCADE")
    )
    resilience_policy_revision_id: Mapped[str] = mapped_column(
        ForeignKey("execution_resilience_policy_revisions.id")
    )
    circuit_version: Mapped[int] = mapped_column(Integer)
    probe_revision: Mapped[int] = mapped_column(Integer)
    domain_kind: Mapped[str] = mapped_column(String(32))
    domain_key: Mapped[str] = mapped_column(String(120))
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("tg_accounts.id"), nullable=True
    )
    dependency_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    owner_token: Mapped[str] = mapped_column(String(80))
    state: Mapped[str] = mapped_column(String(24), default="claimed")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    outcome_code: Mapped[str] = mapped_column(String(80), default="")
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)


class ManagedPresencePolicyRevision(Base):
    __tablename__ = "managed_presence_policy_revisions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "revision", name="uq_managed_presence_policy_revision"
        ),
        Index(
            "uq_managed_presence_policy_active",
            "tenant_id",
            unique=True,
            postgresql_where=text("state = 'active'"),
            sqlite_where=text("state = 'active'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE")
    )
    revision: Mapped[int] = mapped_column(Integer, default=1)
    max_consecutive_system_turns: Mapped[int] = mapped_column(Integer, default=2)
    absolute_daily_authored_cap: Mapped[int] = mapped_column(Integer, default=20)
    managed_to_external_ratio_bps: Mapped[int] = mapped_column(Integer, default=10000)
    bootstrap_allowance: Mapped[int] = mapped_column(Integer, default=2)
    state: Mapped[str] = mapped_column(String(24), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class PostSendVisibilityPolicyRevision(Base):
    __tablename__ = "post_send_visibility_policy_revisions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "revision", name="uq_post_send_visibility_policy_revision"
        ),
        Index(
            "uq_post_send_visibility_policy_active",
            "tenant_id",
            unique=True,
            postgresql_where=text("state = 'active'"),
            sqlite_where=text("state = 'active'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE")
    )
    revision: Mapped[int] = mapped_column(Integer, default=1)
    normal_window_seconds: Mapped[int] = mapped_column(Integer, default=15)
    elevated_window_seconds: Mapped[int] = mapped_column(Integer, default=90)
    state: Mapped[str] = mapped_column(String(24), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class PostSendVisibilityObservation(Base):
    __tablename__ = "post_send_visibility_observations"
    __table_args__ = (
        UniqueConstraint(
            "action_id",
            "attempt_id",
            "policy_revision_id",
            name="uq_post_send_visibility_action_attempt_policy",
        ),
        Index(
            "ix_post_send_visibility_due",
            "state",
            "deadline_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE")
    )
    policy_revision_id: Mapped[str] = mapped_column(
        ForeignKey("post_send_visibility_policy_revisions.id")
    )
    action_id: Mapped[str] = mapped_column(
        ForeignKey("actions.id", ondelete="CASCADE")
    )
    attempt_id: Mapped[str] = mapped_column(
        ForeignKey("execution_attempts.id", ondelete="CASCADE")
    )
    remote_message_id: Mapped[str] = mapped_column(String(160))
    target_peer: Mapped[str] = mapped_column(String(160))
    accepted_content_hash: Mapped[str] = mapped_column(String(64), default="")
    state: Mapped[str] = mapped_column(String(40), default="visibility_pending")
    observer_route: Mapped[str] = mapped_column(String(80), default="gateway_get_messages")
    observer_watermark: Mapped[str] = mapped_column(String(160), default="")
    observer_gap: Mapped[dict] = mapped_column(JSON, default=dict)
    checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    terminal_reason: Mapped[str] = mapped_column(String(80), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now, onupdate=now
    )


__all__ = [
    "AccountBehaviorBudgetLedger",
    "AccountBehaviorBudgetPolicyRevision",
    "AccountBehaviorBudgetReservation",
    "AccountPoolConcurrencyLease",
    "AccountPoolConcurrencyPolicyRevision",
    "ExecutionResiliencePolicyRevision",
    "ExecutionCircuitState",
    "HealthProbeAttempt",
    "ManagedPresencePolicyRevision",
    "PostSendVisibilityObservation",
    "PostSendVisibilityPolicyRevision",
    "RemoteInvocationFence",
]

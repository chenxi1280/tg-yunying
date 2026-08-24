from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import Boolean, CheckConstraint, DateTime, Float, ForeignKey, Index, Integer, JSON, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

from .enums import now


def _uuid() -> str:
    return str(uuid4())


class AiContentWindowPlan(Base):
    __tablename__ = "ai_content_window_plans"
    __table_args__ = (
        CheckConstraint(
            "scope_type IN ('group','comment_source')",
            name="ck_ai_content_window_scope_type",
        ),
        UniqueConstraint(
            "tenant_id",
            "task_id",
            "task_lifecycle_epoch",
            "scope_type",
            "scope_id",
            "pacing_plan_hash",
            "period_key",
            "window_start_at",
            "window_end_at",
            "task_config_revision",
            "content_policy_hash",
            name="uq_ai_content_window_scope",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    task_lifecycle_epoch: Mapped[int] = mapped_column(Integer, default=1)
    scope_type: Mapped[str] = mapped_column(String(24))
    scope_id: Mapped[str] = mapped_column(String(160))
    pacing_plan_hash: Mapped[str] = mapped_column(String(64))
    period_key: Mapped[str] = mapped_column(String(80))
    window_start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    window_end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    task_config_revision: Mapped[int] = mapped_column(Integer)
    content_policy_hash: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(24), default="draft")
    version: Mapped[int] = mapped_column(Integer, default=1)
    plan_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AiContentWindowPlanSlot(Base):
    __tablename__ = "ai_content_window_plan_slots"
    __table_args__ = (
        UniqueConstraint(
            "plan_id",
            "slot_ordinal",
            "slot_revision",
            name="uq_ai_content_window_slot_revision",
        ),
        Index(
            "uq_ai_content_window_current_obligation",
            "obligation_type",
            "obligation_id",
            unique=True,
            postgresql_where=text("state IN ('frozen','claimed','candidate_ready','gateway_bound')"),
            sqlite_where=text("state IN ('frozen','claimed','candidate_ready','gateway_bound')"),
        ),
        Index("ix_ai_content_window_slot_claim", "state", "due_at", "lease_expires_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    plan_id: Mapped[str] = mapped_column(
        ForeignKey("ai_content_window_plans.id", ondelete="CASCADE")
    )
    slot_ordinal: Mapped[int] = mapped_column(Integer)
    slot_revision: Mapped[int] = mapped_column(Integer, default=1)
    obligation_type: Mapped[str] = mapped_column(String(48))
    obligation_id: Mapped[str] = mapped_column(String(255))
    generation_sequence: Mapped[int] = mapped_column(Integer)
    account_id: Mapped[int] = mapped_column(ForeignKey("tg_accounts.id", ondelete="CASCADE"))
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    context_scope_revision: Mapped[int] = mapped_column(Integer)
    context_snapshot_hash: Mapped[str] = mapped_column(String(64))
    context_route: Mapped[str] = mapped_column(String(40))
    content_mode: Mapped[str] = mapped_column(String(64))
    route_evidence_hash: Mapped[str] = mapped_column(String(64))
    prompt_contract_version: Mapped[str] = mapped_column(String(80))
    state: Mapped[str] = mapped_column(String(24), default="frozen")
    claimed_by_job_id: Mapped[str | None] = mapped_column(
        ForeignKey("generation_jobs.id", ondelete="SET NULL"),
        nullable=True,
    )
    lease_epoch: Mapped[int] = mapped_column(Integer, default=0)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class TenantAiProviderRouteSet(Base):
    __tablename__ = "tenant_ai_provider_route_sets"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "purpose",
            "revision",
            name="uq_tenant_ai_provider_route_revision",
        ),
        Index(
            "uq_tenant_ai_provider_route_active",
            "tenant_id",
            "purpose",
            unique=True,
            postgresql_where=text("status = 'active'"),
            sqlite_where=text("status = 'active'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    purpose: Mapped[str] = mapped_column(String(64))
    revision: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24), default="draft")
    content_hash: Mapped[str] = mapped_column(String(64))
    approved_by: Mapped[str] = mapped_column(String(160), default="")
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class TenantAiProviderRouteItem(Base):
    __tablename__ = "tenant_ai_provider_route_items"
    __table_args__ = (
        UniqueConstraint("route_set_id", "priority", name="uq_tenant_ai_route_item_priority"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    route_set_id: Mapped[str] = mapped_column(
        ForeignKey("tenant_ai_provider_route_sets.id", ondelete="CASCADE")
    )
    priority: Mapped[int] = mapped_column(Integer)
    provider_id: Mapped[int] = mapped_column(ForeignKey("ai_providers.id", ondelete="RESTRICT"))
    model_name: Mapped[str] = mapped_column(String(120))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    timeout_ms: Mapped[int] = mapped_column(Integer, default=30000)
    rate_policy: Mapped[dict] = mapped_column(JSON, default=dict)
    concurrency_policy: Mapped[dict] = mapped_column(JSON, default=dict)


class AiProviderAttempt(Base):
    __tablename__ = "ai_provider_attempts"
    __table_args__ = (
        UniqueConstraint(
            "generation_job_id",
            "purpose",
            "attempt_index",
            name="uq_ai_provider_attempt_job_index",
        ),
        Index("ix_ai_provider_attempt_route", "route_set_id", "provider_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    generation_job_id: Mapped[str] = mapped_column(
        ForeignKey("generation_jobs.id", ondelete="CASCADE")
    )
    purpose: Mapped[str] = mapped_column(String(64))
    route_set_id: Mapped[str] = mapped_column(
        ForeignKey("tenant_ai_provider_route_sets.id", ondelete="RESTRICT")
    )
    route_set_revision: Mapped[int] = mapped_column(Integer)
    provider_id: Mapped[int] = mapped_column(ForeignKey("ai_providers.id", ondelete="RESTRICT"))
    model_name: Mapped[str] = mapped_column(String(120))
    priority: Mapped[int] = mapped_column(Integer)
    attempt_index: Mapped[int] = mapped_column(Integer)
    request_hash: Mapped[str] = mapped_column(String(64))
    outcome: Mapped[str] = mapped_column(String(40))
    error_code: Mapped[str] = mapped_column(String(80), default="")
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cached_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_amount: Mapped[float] = mapped_column(Float, default=0.0)
    currency: Mapped[str] = mapped_column(String(16), default="CNY")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class FulfillmentShortfallFact(Base):
    __tablename__ = "fulfillment_shortfall_facts"
    __table_args__ = (
        UniqueConstraint(
            "owner_type",
            "owner_id",
            "period_key",
            name="uq_fulfillment_shortfall_owner_period",
        ),
        Index("ix_fulfillment_shortfall_task", "tenant_id", "task_id", "settled_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    task_lifecycle_epoch: Mapped[int] = mapped_column(Integer, default=1)
    owner_type: Mapped[str] = mapped_column(String(48))
    owner_id: Mapped[str] = mapped_column(String(255))
    period_key: Mapped[str] = mapped_column(String(80))
    kind: Mapped[str] = mapped_column(String(32))
    reason_code: Mapped[str] = mapped_column(String(80))
    requested_quantity: Mapped[int] = mapped_column(Integer)
    settled_quantity: Mapped[int] = mapped_column(Integer)
    evidence_hash: Mapped[str] = mapped_column(String(64))
    settled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


__all__ = [
    "AiContentWindowPlan",
    "AiContentWindowPlanSlot",
    "AiProviderAttempt",
    "FulfillmentShortfallFact",
    "TenantAiProviderRouteItem",
    "TenantAiProviderRouteSet",
]

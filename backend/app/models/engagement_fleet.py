from __future__ import annotations

from datetime import date, datetime
from uuid import uuid4

from sqlalchemy import Date, DateTime, ForeignKey, Index, Integer, JSON, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

from .enums import now


def _new_uuid() -> str:
    return str(uuid4())


class AccountFleetActivityPolicyRevision(Base):
    __tablename__ = "account_fleet_activity_policy_revisions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "account_pool_id", "revision",
            name="uq_fleet_activity_policy_revision",
        ),
        Index(
            "uq_fleet_activity_policy_active",
            "tenant_id", "account_pool_id", unique=True,
            postgresql_where=text("state = 'active'"),
            sqlite_where=text("state = 'active'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    account_pool_id: Mapped[int] = mapped_column(
        ForeignKey("account_pools.id", ondelete="CASCADE")
    )
    revision: Mapped[int] = mapped_column(Integer, default=1)
    period_kind: Mapped[str] = mapped_column(String(32), default="calendar_day")
    rolling_window_days: Mapped[int] = mapped_column(Integer, default=3)
    required_activity_classes: Mapped[list[str]] = mapped_column(JSON, default=list)
    class_targets: Mapped[dict] = mapped_column(JSON, default=dict)
    union_policy: Mapped[str] = mapped_column(
        String(64), default="any_confirmed_business_operation"
    )
    classification_policy_revision: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(24), default="active")
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    effective_to: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class AccountFleetActivityLedger(Base):
    __tablename__ = "account_fleet_activity_ledgers"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "account_pool_id", "account_id", "period_start", "period_end",
            name="uq_fleet_activity_account_period",
        ),
        Index(
            "ix_fleet_activity_pool_period",
            "tenant_id", "account_pool_id", "period_start", "account_id",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    account_pool_id: Mapped[int] = mapped_column(
        ForeignKey("account_pools.id", ondelete="CASCADE")
    )
    account_id: Mapped[int] = mapped_column(
        ForeignKey("tg_accounts.id", ondelete="CASCADE")
    )
    policy_revision_id: Mapped[str] = mapped_column(
        ForeignKey("account_fleet_activity_policy_revisions.id")
    )
    period_kind: Mapped[str] = mapped_column(String(32), default="calendar_day")
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    activity_counts: Mapped[dict] = mapped_column(JSON, default=dict)
    latest_activity_at: Mapped[dict] = mapped_column(JSON, default=dict)
    qualified_activity_classes: Mapped[list[str]] = mapped_column(JSON, default=list)
    required_status: Mapped[dict] = mapped_column(JSON, default=dict)
    fairness_debt: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AccountFleetActivityFactProjection(Base):
    __tablename__ = "account_fleet_activity_fact_projections"
    __table_args__ = (
        UniqueConstraint(
            "account_pool_id", "account_id", "activity_class",
            "source_fact_kind", "source_fact_id",
            name="uq_fleet_activity_source_fact",
        ),
        Index(
            "ix_fleet_activity_projection_timeline",
            "tenant_id", "account_pool_id", "account_id", "observed_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    account_pool_id: Mapped[int] = mapped_column(
        ForeignKey("account_pools.id", ondelete="CASCADE")
    )
    account_id: Mapped[int] = mapped_column(
        ForeignKey("tg_accounts.id", ondelete="CASCADE")
    )
    ledger_id: Mapped[str] = mapped_column(
        ForeignKey("account_fleet_activity_ledgers.id", ondelete="CASCADE")
    )
    policy_revision_id: Mapped[str] = mapped_column(
        ForeignKey("account_fleet_activity_policy_revisions.id")
    )
    task_id: Mapped[str] = mapped_column(String(36))
    action_id: Mapped[str] = mapped_column(String(36))
    activity_class: Mapped[str] = mapped_column(String(48))
    source_fact_kind: Mapped[str] = mapped_column(String(48))
    source_fact_id: Mapped[str] = mapped_column(String(80))
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


__all__ = [
    "AccountFleetActivityFactProjection",
    "AccountFleetActivityLedger",
    "AccountFleetActivityPolicyRevision",
]

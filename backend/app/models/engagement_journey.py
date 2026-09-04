from __future__ import annotations

from datetime import date, datetime
from uuid import uuid4

from sqlalchemy import Date, DateTime, ForeignKey, Index, Integer, JSON, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

from .enums import now


def _new_uuid() -> str:
    return str(uuid4())


class CrossAdapterSourceJourneyPlanRevision(Base):
    __tablename__ = "cross_adapter_source_journey_plan_revisions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "source_revision_id",
            "task_day",
            "plan_revision",
            name="uq_source_journey_plan_revision",
        ),
        Index(
            "uq_source_journey_plan_active",
            "tenant_id",
            "source_revision_id",
            "task_day",
            unique=True,
            postgresql_where=text("state = 'active'"),
            sqlite_where=text("state = 'active'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE")
    )
    source_revision_id: Mapped[str] = mapped_column(
        ForeignKey("channel_message_source_revisions.id", ondelete="CASCADE")
    )
    task_day: Mapped[date] = mapped_column(Date)
    plan_revision: Mapped[int] = mapped_column(Integer, default=1)
    source_task_set_hash: Mapped[str] = mapped_column(String(64))
    policy_revision: Mapped[str] = mapped_column(String(64))
    adapter_constraints: Mapped[list[dict]] = mapped_column(JSON, default=list)
    hard_constraint_hash: Mapped[str] = mapped_column(String(64))
    objective_policy: Mapped[dict] = mapped_column(JSON, default=dict)
    edge_set: Mapped[list[dict]] = mapped_column(JSON, default=list)
    edge_set_hash: Mapped[str] = mapped_column(String(64))
    overlap_metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    deficits: Mapped[list[dict]] = mapped_column(JSON, default=list)
    decision: Mapped[str] = mapped_column(String(40))
    input_hash: Mapped[str] = mapped_column(String(64))
    supersedes_plan_id: Mapped[str | None] = mapped_column(
        ForeignKey("cross_adapter_source_journey_plan_revisions.id"), nullable=True
    )
    state: Mapped[str] = mapped_column(String(24), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class SourceJourneyDecision(Base):
    __tablename__ = "source_journey_decisions"
    __table_args__ = (
        UniqueConstraint(
            "plan_id",
            "task_id",
            "action_class",
            "account_id",
            name="uq_source_journey_decision_edge",
        ),
        Index(
            "ix_source_journey_decision_task",
            "task_id",
            "action_class",
            "decision_state",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE")
    )
    plan_id: Mapped[str] = mapped_column(
        ForeignKey(
            "cross_adapter_source_journey_plan_revisions.id", ondelete="CASCADE"
        )
    )
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    account_id: Mapped[int] = mapped_column(
        ForeignKey("tg_accounts.id", ondelete="CASCADE")
    )
    action_class: Mapped[str] = mapped_column(String(40))
    journey_class: Mapped[str] = mapped_column(String(40))
    decision_state: Mapped[str] = mapped_column(String(24), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


__all__ = [
    "CrossAdapterSourceJourneyPlanRevision",
    "SourceJourneyDecision",
]

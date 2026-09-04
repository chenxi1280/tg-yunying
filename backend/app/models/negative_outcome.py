from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, Index, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from .enums import now


def _new_uuid() -> str:
    return str(uuid4())


class NegativeOutcomePolicyRevision(Base):
    __tablename__ = "negative_outcome_policy_revisions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "revision", name="uq_negative_outcome_policy_revision"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    state: Mapped[str] = mapped_column(String(32), default="active")
    event_types: Mapped[list] = mapped_column(JSON, default=list)
    proactive_throttled_threshold: Mapped[int] = mapped_column(Integer, default=1)
    response_restricted_threshold: Mapped[int] = mapped_column(Integer, default=2)
    quarantine_threshold: Mapped[int] = mapped_column(Integer, default=3)
    manual_review_threshold: Mapped[int] = mapped_column(Integer, default=5)
    minimum_hold_seconds: Mapped[int] = mapped_column(Integer, default=300)
    recovery_window_seconds: Mapped[int] = mapped_column(Integer, default=1800)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class NegativeOutcomeCircuitState(Base):
    __tablename__ = "negative_outcome_circuit_states"
    __table_args__ = (
        UniqueConstraint("tenant_id", "route", "peer_id", "account_id", name="uq_negative_outcome_circuit_scope"),
        Index("ix_negative_outcome_circuit_level", "tenant_id", "level"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    peer_id: Mapped[str] = mapped_column(String(120), default="")
    route: Mapped[str] = mapped_column(String(40), default="")
    account_id: Mapped[int | None] = mapped_column(ForeignKey("tg_accounts.id", ondelete="CASCADE"), nullable=True)
    level: Mapped[str] = mapped_column(String(32), default="normal")
    events: Mapped[list] = mapped_column(JSON, default=list)
    entered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    eligible_exit_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reason: Mapped[str] = mapped_column(String(80), default="")
    policy_revision_id: Mapped[str] = mapped_column(String(36), default="")
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


__all__ = [
    "NegativeOutcomePolicyRevision",
    "NegativeOutcomeCircuitState",
]

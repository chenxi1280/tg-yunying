from __future__ import annotations

from datetime import date, datetime
from uuid import uuid4

from sqlalchemy import Date, DateTime, Float, ForeignKey, Index, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

from .enums import now


def new_uuid() -> str:
    return str(uuid4())


class SearchJoinPacingDecision(Base):
    __tablename__ = "search_join_pacing_decisions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "task_id", "decision_scope", "scope_key", name="uq_search_join_pacing_decision_scope"),
        Index("ix_search_join_pacing_decision_task", "tenant_id", "task_id", "local_date"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), default=1)
    task_id: Mapped[str] = mapped_column(String(36))
    decision_scope: Mapped[str] = mapped_column(String(24), default="")
    scope_key: Mapped[str] = mapped_column(String(160), default="")
    tenant_timezone: Mapped[str] = mapped_column(String(50), default="Asia/Shanghai")
    local_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    window_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    account_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    keyword_hash: Mapped[str] = mapped_column(String(64), default="")
    sampled_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    threshold: Mapped[float | None] = mapped_column(Float, nullable=True)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reason: Mapped[str] = mapped_column(String(120), default="")
    decision_value: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


__all__ = ["SearchJoinPacingDecision"]

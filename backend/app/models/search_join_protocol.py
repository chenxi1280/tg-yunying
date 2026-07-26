from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, Index, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

from .enums import now


def _new_uuid() -> str:
    return str(uuid4())


class SearchJoinProtocolTrace(Base):
    __tablename__ = "search_join_protocol_traces"
    __table_args__ = (
        UniqueConstraint("action_id", "recovery_kind", name="uq_search_join_protocol_trace_recovery"),
        Index("ix_search_join_protocol_trace_task_created", "task_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    action_id: Mapped[str] = mapped_column(ForeignKey("actions.id", ondelete="CASCADE"))
    bot_username: Mapped[str] = mapped_column(String(80), default="")
    protocol_sample_version: Mapped[str] = mapped_column(String(40), default="")
    recovery_kind: Mapped[str] = mapped_column(String(40), default="initial")
    status: Mapped[str] = mapped_column(String(40), default="observed")
    event_type: Mapped[str] = mapped_column(String(60), default="page_classified")
    attempt_no: Mapped[int] = mapped_column(Integer, default=0)
    page_phase: Mapped[str] = mapped_column(String(60), default="unknown_page")
    post_reset_page_phase: Mapped[str] = mapped_column(String(60), default="")
    trace_summary: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


__all__ = ["SearchJoinProtocolTrace"]

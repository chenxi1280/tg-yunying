"""Immutable content successor for an existing group-message obligation."""
from datetime import datetime
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from .enums import now


class AiGroupEmergencySelection(Base):
    __tablename__ = "ai_group_emergency_selections"
    __table_args__ = (
        UniqueConstraint("action_id", name="uq_ai_group_emergency_action"),
        UniqueConstraint("primary_quantity_slot_id", name="uq_ai_group_emergency_quantity"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="RESTRICT"))
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="RESTRICT"))
    action_id: Mapped[str] = mapped_column(ForeignKey("actions.id", ondelete="RESTRICT"))
    primary_quantity_slot_id: Mapped[str] = mapped_column(
        ForeignKey("task_group_daily_message_slots.id", ondelete="RESTRICT"))
    generation_job_id: Mapped[str | None] = mapped_column(
        ForeignKey("generation_jobs.id", ondelete="RESTRICT"), nullable=True)
    policy_version: Mapped[str] = mapped_column(String(48))
    previous_action_version: Mapped[int] = mapped_column(Integer)
    materialization_version: Mapped[int] = mapped_column(Integer)
    previous_payload_hash: Mapped[str] = mapped_column(String(64))
    content_hash: Mapped[str] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(48))
    reason: Mapped[str] = mapped_column(String(80))
    identity: Mapped[dict] = mapped_column(JSON)
    evidence: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


__all__ = ["AiGroupEmergencySelection"]

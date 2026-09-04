from datetime import datetime
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from .enums import now


class ChannelTaskIntake(Base):
    __tablename__ = "channel_task_intakes"
    __table_args__ = (UniqueConstraint("task_id", "lifecycle_epoch", "channel_target_id", name="uq_channel_task_intake"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"))
    lifecycle_epoch: Mapped[int] = mapped_column(Integer)
    channel_target_id: Mapped[int] = mapped_column(ForeignKey("operation_targets.id"))
    anchor_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    initial_source_keys: Mapped[list] = mapped_column(JSON, default=list)
    historical_limit: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ChannelSourceDecision(Base):
    __tablename__ = "channel_source_decisions"
    __table_args__ = (UniqueConstraint("intake_id", "channel_message_id", name="uq_channel_source_decision"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    intake_id: Mapped[str] = mapped_column(ForeignKey("channel_task_intakes.id"))
    channel_message_id: Mapped[int] = mapped_column(ForeignKey("channel_messages.id"))
    source_key: Mapped[str] = mapped_column(String(100))
    decision: Mapped[str] = mapped_column(String(48))
    reason: Mapped[str] = mapped_column(String(80), default="")
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

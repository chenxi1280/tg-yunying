from __future__ import annotations

from datetime import date, datetime
from uuid import uuid4

from sqlalchemy import Date, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

from .enums import now


def _new_uuid() -> str:
    return str(uuid4())


class ChannelViewDailyIdentityOwner(Base):
    __tablename__ = "channel_view_daily_identity_owners"
    __table_args__ = (
        UniqueConstraint(
            "target_peer_id",
            "channel_message_id",
            "account_id",
            "obligation_local_date",
            name="uq_channel_view_daily_identity",
        ),
        UniqueConstraint(
            "obligation_id",
            name="uq_channel_view_daily_identity_obligation",
        ),
        UniqueConstraint(
            "action_id",
            name="uq_channel_view_daily_identity_action",
        ),
        Index(
            "ix_channel_view_daily_identity_state",
            "obligation_local_date",
            "state",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    target_peer_id: Mapped[str] = mapped_column(String(120), nullable=False)
    channel_message_id: Mapped[int] = mapped_column(Integer, nullable=False)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("tg_accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    obligation_local_date: Mapped[date] = mapped_column(Date, nullable=False)
    state: Mapped[str] = mapped_column(String(24), default="pre_gateway", nullable=False)
    logical_task_id: Mapped[str] = mapped_column(String(36), nullable=False)
    obligation_id: Mapped[str | None] = mapped_column(
        ForeignKey("view_fulfillment_obligations.id", ondelete="SET NULL"),
        nullable=True,
    )
    action_id: Mapped[str | None] = mapped_column(
        ForeignKey("actions.id", ondelete="SET NULL"),
        nullable=True,
    )
    request_identity: Mapped[str] = mapped_column(String(160), nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=now,
        onupdate=now,
    )


__all__ = ["ChannelViewDailyIdentityOwner"]

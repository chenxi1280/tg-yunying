from datetime import datetime
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from .enums import now


class AlbumReactionParticipation(Base):
    __tablename__ = "album_reaction_participations"
    __table_args__ = (UniqueConstraint("task_id", "lifecycle_epoch", "channel_target_id", "album_id", "account_id", name="uq_album_reaction_participant"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"))
    lifecycle_epoch: Mapped[int] = mapped_column(Integer)
    task_day_ledger_id: Mapped[str] = mapped_column(ForeignKey("task_day_ledgers.id"))
    channel_target_id: Mapped[int] = mapped_column(ForeignKey("operation_targets.id"))
    target_peer_id: Mapped[str] = mapped_column(String(160))
    album_id: Mapped[str] = mapped_column(String(64))
    account_id: Mapped[int] = mapped_column(ForeignKey("tg_accounts.id"))
    source_revision_hash: Mapped[str] = mapped_column(String(64))
    children: Mapped[list] = mapped_column(JSON)
    child_count: Mapped[int] = mapped_column(Integer)
    child_count_reason: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(40), default="pending")
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

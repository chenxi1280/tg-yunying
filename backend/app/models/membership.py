from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

from .enums import now


class TargetMembershipChallengeAttempt(Base):
    __tablename__ = "target_membership_challenge_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    verification_task_id: Mapped[int | None] = mapped_column(ForeignKey("verification_tasks.id"), nullable=True)
    membership_item_id: Mapped[str] = mapped_column(String(120), default="")
    account_id: Mapped[int | None] = mapped_column(ForeignKey("tg_accounts.id"), nullable=True)
    group_id: Mapped[int | None] = mapped_column(ForeignKey("tg_groups.id"), nullable=True)
    challenge_type: Mapped[str] = mapped_column(String(60), default="")
    question_hash: Mapped[str] = mapped_column(String(128), default="")
    question_snapshot: Mapped[str] = mapped_column(Text, default="")
    context_status: Mapped[str] = mapped_column(String(40), default="")
    context_message_count: Mapped[int] = mapped_column(Integer, default=0)
    context_failure_detail: Mapped[str] = mapped_column(Text, default="")
    media_message_id: Mapped[str] = mapped_column(String(120), default="")
    media_fingerprint: Mapped[str] = mapped_column(String(128), default="")
    media_mime_type: Mapped[str] = mapped_column(String(80), default="")
    answer_source: Mapped[str] = mapped_column(String(80), default="")
    answer_text: Mapped[str] = mapped_column(String(160), default="")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    model_name: Mapped[str] = mapped_column(String(120), default="")
    attempt_no: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(40), default="")
    result_snapshot: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(120), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


__all__ = ["TargetMembershipChallengeAttempt"]

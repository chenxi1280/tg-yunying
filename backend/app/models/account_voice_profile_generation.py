from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

from .enums import now


def _new_uuid() -> str:
    return str(uuid4())


OPEN_GENERATION_ITEM_STATUSES = ("queued", "generating", "validating", "retry_wait", "persist_unknown")
OPEN_GENERATION_ITEM_STATUS_SQL = "status IN ('queued', 'generating', 'validating', 'retry_wait', 'persist_unknown')"


class AiAccountVoiceProfileGenerationJob(Base):
    __tablename__ = "ai_account_voice_profile_generation_jobs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_ai_voice_profile_generation_job_idempotency"),
        Index("ix_ai_voice_profile_generation_jobs_tenant_status", "tenant_id", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    source: Mapped[str] = mapped_column(String(40), nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="queued")
    requested_by: Mapped[str] = mapped_column(String(120), default="system")
    reason: Mapped[str] = mapped_column(Text, default="")
    total_count: Mapped[int] = mapped_column(Integer, default=0)
    succeeded_count: Mapped[int] = mapped_column(Integer, default=0)
    retry_wait_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AiAccountVoiceProfileGenerationItem(Base):
    __tablename__ = "ai_account_voice_profile_generation_items"
    __table_args__ = (
        UniqueConstraint("job_id", "account_id", name="uq_ai_voice_profile_generation_job_account"),
        UniqueConstraint("tenant_id", "operator_idempotency_key", name="uq_ai_voice_profile_generation_item_operator_key"),
        Index(
            "uq_ai_voice_profile_generation_open_account",
            "tenant_id",
            "account_id",
            unique=True,
            sqlite_where=text(OPEN_GENERATION_ITEM_STATUS_SQL),
            postgresql_where=text(OPEN_GENERATION_ITEM_STATUS_SQL),
        ),
        Index("ix_ai_voice_profile_generation_items_due", "status", "next_retry_at", "created_at"),
        Index("ix_ai_voice_profile_generation_items_lease", "status", "lease_expires_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    job_id: Mapped[str] = mapped_column(ForeignKey("ai_account_voice_profile_generation_jobs.id", ondelete="CASCADE"), nullable=False)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    account_id: Mapped[int] = mapped_column(ForeignKey("tg_accounts.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="queued")
    idempotency_key: Mapped[str] = mapped_column(String(120), default="")
    expected_profile_version: Mapped[int] = mapped_column(Integer, default=1)
    base_profile_version: Mapped[int] = mapped_column(Integer, default=0)
    result_profile_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str] = mapped_column(String(100), default="")
    error_detail: Mapped[str] = mapped_column(Text, default="")
    provider_request_id: Mapped[str] = mapped_column(String(160), default="")
    lease_owner: Mapped[str] = mapped_column(String(160), default="")
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source: Mapped[str] = mapped_column(String(40), nullable=False)
    previous_item_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    operator_idempotency_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AiAccountVoiceProfileGenerationAttempt(Base):
    __tablename__ = "ai_account_voice_profile_generation_attempts"
    __table_args__ = (
        UniqueConstraint("item_id", "attempt_no", name="uq_ai_voice_profile_generation_attempt_no"),
        Index("ix_ai_voice_profile_generation_attempts_item", "item_id", "attempt_no"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    job_id: Mapped[str] = mapped_column(ForeignKey("ai_account_voice_profile_generation_jobs.id", ondelete="CASCADE"), nullable=False)
    item_id: Mapped[str] = mapped_column(ForeignKey("ai_account_voice_profile_generation_items.id", ondelete="CASCADE"), nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    stage: Mapped[str] = mapped_column(String(40), default="generate")
    provider: Mapped[str] = mapped_column(String(120), default="")
    provider_request_id: Mapped[str] = mapped_column(String(160), default="")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    outcome: Mapped[str] = mapped_column(String(30), default="running")
    error_code: Mapped[str] = mapped_column(String(100), default="")
    error_detail: Mapped[str] = mapped_column(Text, default="")
    prompt_feedback_summary: Mapped[str] = mapped_column(Text, default="")


__all__ = [
    "AiAccountVoiceProfileGenerationAttempt",
    "AiAccountVoiceProfileGenerationItem",
    "AiAccountVoiceProfileGenerationJob",
    "OPEN_GENERATION_ITEM_STATUSES",
]

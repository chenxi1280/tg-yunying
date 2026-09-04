from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ProviderHttpExchange(Base):
    __tablename__ = "provider_http_exchanges"
    __table_args__ = (Index("ix_provider_http_exchange_logical", "logical_request_id", "provider_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    chain_id: Mapped[str] = mapped_column(String(36))
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="RESTRICT"))
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="RESTRICT"))
    task_lifecycle_epoch: Mapped[int] = mapped_column(Integer)
    provider_id: Mapped[int] = mapped_column(ForeignKey("ai_providers.id", ondelete="RESTRICT"))
    logical_request_id: Mapped[str] = mapped_column(String(200))
    model_name: Mapped[str] = mapped_column(String(120))
    purpose: Mapped[str] = mapped_column(String(64))
    request_hash: Mapped[str] = mapped_column(String(64))
    outcome: Mapped[str] = mapped_column(String(32))
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_hash: Mapped[str] = mapped_column(String(64), default="")
    error_code: Mapped[str] = mapped_column(String(80), default="")
    local_termination_confirmed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ProviderHttpExchangeJob(Base):
    __tablename__ = "provider_http_exchange_jobs"
    __table_args__ = (Index("ix_provider_http_exchange_job", "generation_job_id"),)

    exchange_id: Mapped[str] = mapped_column(ForeignKey("provider_http_exchanges.id", ondelete="RESTRICT"), primary_key=True)
    generation_job_id: Mapped[str] = mapped_column(ForeignKey("generation_timing_bindings.generation_job_id", ondelete="RESTRICT"), primary_key=True)
    execution_path_hash: Mapped[str] = mapped_column(String(64))


__all__ = ["ProviderHttpExchange", "ProviderHttpExchangeJob"]

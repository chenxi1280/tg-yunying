from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class GenerationTimingBinding(Base):
    __tablename__ = "generation_timing_bindings"

    generation_job_id: Mapped[str] = mapped_column(ForeignKey("generation_jobs.id", ondelete="RESTRICT"), primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="RESTRICT"))
    task_lifecycle_epoch: Mapped[int] = mapped_column(Integer)
    adapter: Mapped[str] = mapped_column(String(40))
    lane: Mapped[str] = mapped_column(String(40))
    execution_path_hash: Mapped[str] = mapped_column(String(64))
    timing_profile_id: Mapped[str | None] = mapped_column(ForeignKey("execution_timing_profile_revisions.id", ondelete="RESTRICT"))
    profile_snapshot_hash: Mapped[str | None] = mapped_column(String(64))
    resilience_policy_id: Mapped[str | None] = mapped_column(ForeignKey("execution_resilience_policy_revisions.id", ondelete="RESTRICT"))
    llm_timeout_ceiling_seconds: Mapped[int] = mapped_column(Integer)
    bound_send_deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    bound_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


__all__ = ["GenerationTimingBinding"]

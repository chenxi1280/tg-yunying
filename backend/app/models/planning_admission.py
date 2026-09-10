"""Immutable admission identities with tenant-scoped shared account evidence."""
from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, ForeignKeyConstraint, Index, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from .enums import now


def _new_uuid() -> str:
    return str(uuid4())


class PlanningAdmissionEvidence(Base):
    __tablename__ = "planning_admission_evidence"

    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True)
    digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_paths: Mapped[list[dict]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class PlanningAdmissionSnapshot(Base):
    __tablename__ = "planning_admission_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "task_id",
            "task_lifecycle_epoch",
            "participation_unit",
            "planning_horizon",
            "dependency_revision_set_hash",
            name="uq_planning_admission_dependency_set",
        ),
        Index("ix_planning_admission_plan", "participation_plan_id", "decision"),
        ForeignKeyConstraint(
            ["tenant_id", "account_paths_digest"],
            ["planning_admission_evidence.tenant_id", "planning_admission_evidence.digest"],
            name="fk_planning_admission_evidence",
        ),
        Index("ix_planning_admission_evidence", "tenant_id", "account_paths_digest"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    task_lifecycle_epoch: Mapped[int] = mapped_column(Integer, default=1)
    participation_plan_id: Mapped[str] = mapped_column(ForeignKey("task_participation_unit_plans.id", ondelete="CASCADE"))
    participation_unit: Mapped[str] = mapped_column(String(200))
    planning_horizon: Mapped[str] = mapped_column(String(100))
    dependency_revision_set_hash: Mapped[str] = mapped_column(String(64))
    legacy_account_paths: Mapped[list[dict]] = mapped_column("account_paths", JSON, default=list)
    account_paths_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    paths_evidence: Mapped[PlanningAdmissionEvidence | None] = relationship(lazy="selectin", viewonly=True)
    admissible_account_ids: Mapped[list[int]] = mapped_column(JSON, default=list)
    deficit_account_ids: Mapped[list[int]] = mapped_column(JSON, default=list)
    decision: Mapped[str] = mapped_column(String(32))
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


    @property
    def account_paths(self) -> list[dict]:
        if self.account_paths_digest is None:
            return self.legacy_account_paths
        if self.paths_evidence is None:
            raise RuntimeError("planning_admission_evidence_missing")
        return self.paths_evidence.account_paths

    @account_paths.setter
    def account_paths(self, value: list[dict]) -> None:
        if self.account_paths_digest is not None:
            raise ValueError("planning_admission_evidence_is_immutable")
        self.legacy_account_paths = value

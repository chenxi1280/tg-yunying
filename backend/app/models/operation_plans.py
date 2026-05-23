from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

from .enums import now


def new_uuid() -> str:
    return str(uuid4())


class OperationPlanTemplate(Base):
    __tablename__ = "operation_plan_templates"
    __table_args__ = (
        Index("ix_operation_plan_templates_status", "tenant_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), default=1)
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(Text, default="")
    target_type: Mapped[str] = mapped_column(String(30), default="group")
    status: Mapped[str] = mapped_column(String(30), default="active")
    strategy_config: Mapped[dict] = mapped_column(JSON, default=dict)
    task_blueprints: Mapped[list] = mapped_column(JSON, default=list)
    created_by: Mapped[str] = mapped_column(String(100), default="")
    updated_by: Mapped[str] = mapped_column(String(100), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class OperationPlanTarget(Base):
    __tablename__ = "operation_plan_targets"
    __table_args__ = (
        UniqueConstraint("tenant_id", "plan_id", "target_id", name="uq_operation_plan_targets_target"),
        Index("ix_operation_plan_targets_plan", "tenant_id", "plan_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), default=1)
    plan_id: Mapped[int] = mapped_column(ForeignKey("operation_plan_templates.id"))
    target_id: Mapped[int] = mapped_column(ForeignKey("operation_targets.id"))
    status: Mapped[str] = mapped_column(String(30), default="active")
    strategy_config: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class OperationPlanTaskLink(Base):
    __tablename__ = "operation_plan_task_links"
    __table_args__ = (
        UniqueConstraint("tenant_id", "plan_id", "task_id", name="uq_operation_plan_task_links_task"),
        Index("ix_operation_plan_task_links_plan", "tenant_id", "plan_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), default=1)
    plan_id: Mapped[int] = mapped_column(ForeignKey("operation_plan_templates.id"))
    target_id: Mapped[int | None] = mapped_column(ForeignKey("operation_targets.id"), nullable=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"))
    relation: Mapped[str] = mapped_column(String(40), default="generated")
    status: Mapped[str] = mapped_column(String(30), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class OperationPlanGenerationRun(Base):
    __tablename__ = "operation_plan_generation_runs"
    __table_args__ = (
        Index("ix_operation_plan_generation_runs_plan", "tenant_id", "plan_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), default=1)
    plan_id: Mapped[int] = mapped_column(ForeignKey("operation_plan_templates.id"))
    run_type: Mapped[str] = mapped_column(String(40), default="preview")
    status: Mapped[str] = mapped_column(String(30), default="success")
    requested_by: Mapped[str] = mapped_column(String(100), default="")
    request_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    result_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    failure_detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


__all__ = [
    "OperationPlanGenerationRun",
    "OperationPlanTarget",
    "OperationPlanTaskLink",
    "OperationPlanTemplate",
]

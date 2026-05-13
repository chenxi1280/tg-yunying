from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

from .enums import now


class RuleSet(Base):
    __tablename__ = "rule_sets"
    __table_args__ = (UniqueConstraint("tenant_id", "name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), default=1)
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(30), default="active")
    task_types: Mapped[list] = mapped_column(JSON, default=list)
    default_policy: Mapped[dict] = mapped_column(JSON, default=dict)
    active_version_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class RuleSetVersion(Base):
    __tablename__ = "rule_set_versions"
    __table_args__ = (UniqueConstraint("rule_set_id", "version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), default=1)
    rule_set_id: Mapped[int] = mapped_column(ForeignKey("rule_sets.id"))
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(30), default="draft")
    version_note: Mapped[str] = mapped_column(Text, default="")
    filters: Mapped[dict] = mapped_column(JSON, default=dict)
    output_checks: Mapped[dict] = mapped_column(JSON, default=dict)
    transforms: Mapped[dict] = mapped_column(JSON, default=dict)
    routing: Mapped[dict] = mapped_column(JSON, default=dict)
    account_strategy: Mapped[dict] = mapped_column(JSON, default=dict)
    rate_limits: Mapped[dict] = mapped_column(JSON, default=dict)
    retry_policy: Mapped[dict] = mapped_column(JSON, default=dict)
    created_by: Mapped[str] = mapped_column(String(100), default="")
    published_by: Mapped[str] = mapped_column(String(100), default="")
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


__all__ = ["RuleSet", "RuleSetVersion"]

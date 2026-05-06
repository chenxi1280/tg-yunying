"""Shared SQLAlchemy mixins for common model columns."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

from .enums import now


class TenantMixin(Base):
    """Mixin that adds a tenant_id foreign key. Declarative, not a table."""
    __abstract__ = True

    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))


class TimestampMixin(Base):
    """Mixin that adds created_at and updated_at columns."""
    __abstract__ = True

    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class CreatedAtMixin(Base):
    """Mixin that adds only created_at (for append-only models)."""
    __abstract__ = True

    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


__all__ = ["TenantMixin", "TimestampMixin", "CreatedAtMixin"]

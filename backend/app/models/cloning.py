from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

from .enums import now


class AccountClonePlan(Base):
    __tablename__ = "account_clone_plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    source_account_id: Mapped[int] = mapped_column(ForeignKey("tg_accounts.id"))
    target_account_id: Mapped[int] = mapped_column(ForeignKey("tg_accounts.id"))
    clone_scope: Mapped[str] = mapped_column(String(160), default="contacts,groups")
    status: Mapped[str] = mapped_column(String(30), default="待确认")
    items_total: Mapped[int] = mapped_column(Integer, default=0)
    items_done: Mapped[int] = mapped_column(Integer, default=0)
    items_failed: Mapped[int] = mapped_column(Integer, default=0)
    failure_detail: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(100), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AccountCloneItem(Base):
    __tablename__ = "account_clone_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    plan_id: Mapped[int] = mapped_column(ForeignKey("account_clone_plans.id"))
    source_account_id: Mapped[int] = mapped_column(ForeignKey("tg_accounts.id"))
    target_account_id: Mapped[int] = mapped_column(ForeignKey("tg_accounts.id"))
    target_type: Mapped[str] = mapped_column(String(40))
    target_peer_id: Mapped[str] = mapped_column(String(120))
    target_display: Mapped[str] = mapped_column(String(160), default="")
    status: Mapped[str] = mapped_column(String(30), default="待确认")
    failure_type: Mapped[str] = mapped_column(String(40), default="")
    failure_detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


__all__ = ["AccountCloneItem", "AccountClonePlan"]

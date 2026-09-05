"""Append-only membership and group-state evidence, retained after pool deletion."""
from datetime import datetime
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from .enums import now


def _new_uuid():
    return str(uuid4())


class AccountGroupMembershipRevision(Base):
    __tablename__ = "account_group_membership_revisions"
    __table_args__ = (UniqueConstraint("tenant_id", "account_pool_id", "revision",
        name="uq_account_group_membership_revision"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    # Historical pool identity must survive an otherwise permitted hard deletion.
    account_pool_id: Mapped[int] = mapped_column(Integer)
    revision: Mapped[int] = mapped_column(Integer)
    member_account_ids: Mapped[list] = mapped_column(JSON)
    member_contracts: Mapped[list] = mapped_column(JSON)
    member_set_hash: Mapped[str] = mapped_column(String(64))
    membership_hash: Mapped[str] = mapped_column(String(64))
    supersedes_revision_id: Mapped[str | None] = mapped_column(
        ForeignKey("account_group_membership_revisions.id"), nullable=True)
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    actor: Mapped[str] = mapped_column(String(100))
    reason: Mapped[str] = mapped_column(String(160))


class AccountGroupStateRevision(Base):
    __tablename__ = "account_group_state_revisions"
    __table_args__ = (UniqueConstraint("tenant_id", "account_pool_id", "revision",
        name="uq_account_group_state_revision"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    account_pool_id: Mapped[int] = mapped_column(Integer)
    revision: Mapped[int] = mapped_column(Integer)
    group_state: Mapped[dict] = mapped_column(JSON)
    state_hash: Mapped[str] = mapped_column(String(64))
    supersedes_revision_id: Mapped[str | None] = mapped_column(
        ForeignKey("account_group_state_revisions.id"), nullable=True)
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    actor: Mapped[str] = mapped_column(String(100))
    reason: Mapped[str] = mapped_column(String(160))


__all__ = ["AccountGroupMembershipRevision", "AccountGroupStateRevision"]

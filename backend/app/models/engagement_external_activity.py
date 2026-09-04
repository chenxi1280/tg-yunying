from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, Index, Integer, JSON, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

from .enums import now


def _new_uuid() -> str:
    return str(uuid4())


class ExternalAccountUsePolicyRevision(Base):
    __tablename__ = "external_account_use_policy_revisions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "revision", name="uq_external_account_use_policy_revision"
        ),
        Index(
            "uq_external_account_use_policy_active",
            "tenant_id",
            unique=True,
            postgresql_where=text("state = 'active'"),
            sqlite_where=text("state = 'active'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE")
    )
    revision: Mapped[int] = mapped_column(Integer, default=1)
    hold_seconds_by_class: Mapped[dict] = mapped_column(JSON, default=dict)
    collision_classes_by_class: Mapped[dict] = mapped_column(JSON, default=dict)
    state: Mapped[str] = mapped_column(String(24), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class UnownedOutboundActivityObservation(Base):
    __tablename__ = "unowned_outbound_activity_observations"
    __table_args__ = (
        UniqueConstraint(
            "activity_identity_hash",
            name="uq_unowned_outbound_activity_identity",
        ),
        Index(
            "ix_unowned_outbound_account_time",
            "tenant_id",
            "account_id",
            "observed_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE")
    )
    account_id: Mapped[int] = mapped_column(
        ForeignKey("tg_accounts.id", ondelete="CASCADE")
    )
    activity_class: Mapped[str] = mapped_column(String(40))
    canonical_peer_id: Mapped[str] = mapped_column(String(160))
    canonical_source_identity: Mapped[str] = mapped_column(String(200), default="")
    remote_identity: Mapped[str] = mapped_column(String(160))
    activity_identity_hash: Mapped[str] = mapped_column(String(64))
    source_kind: Mapped[str] = mapped_column(String(40), default="telegram_update")
    source_event_id: Mapped[str] = mapped_column(String(80), default="")
    ownership_state: Mapped[str] = mapped_column(String(32), default="unowned")
    ownership_evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AccountExternalUseHold(Base):
    __tablename__ = "account_external_use_holds"
    __table_args__ = (
        UniqueConstraint(
            "observation_id",
            name="uq_account_external_use_hold_observation",
        ),
        Index(
            "ix_account_external_use_hold_active",
            "tenant_id",
            "account_id",
            "action_class",
            "state",
            "expires_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE")
    )
    account_id: Mapped[int] = mapped_column(
        ForeignKey("tg_accounts.id", ondelete="CASCADE")
    )
    observation_id: Mapped[str] = mapped_column(
        ForeignKey(
            "unowned_outbound_activity_observations.id",
            ondelete="CASCADE",
        )
    )
    policy_revision_id: Mapped[str] = mapped_column(
        ForeignKey("external_account_use_policy_revisions.id")
    )
    canonical_peer_id: Mapped[str] = mapped_column(String(160))
    canonical_source_identity: Mapped[str] = mapped_column(String(200), default="")
    action_class: Mapped[str] = mapped_column(String(40))
    collision_action_classes: Mapped[list] = mapped_column(JSON, default=list)
    reason_code: Mapped[str] = mapped_column(
        String(80), default="unowned_outbound_activity"
    )
    state: Mapped[str] = mapped_column(String(24), default="active")
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    released_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


__all__ = [
    "AccountExternalUseHold",
    "ExternalAccountUsePolicyRevision",
    "UnownedOutboundActivityObservation",
]

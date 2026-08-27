from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

from .enums import now


TERMINAL_FULL_INIT_STATUSES = (
    "succeeded",
    "failed",
    "manual_required",
    "reconcile_unknown",
    "cancelled",
)
ACTIVE_FULL_INIT_SQL = (
    "status NOT IN ('succeeded','failed','manual_required','reconcile_unknown','cancelled')"
)


class TgAccountFullInitialization(Base):
    __tablename__ = "tg_account_full_initializations"
    __table_args__ = (
        UniqueConstraint("account_id", "generation", name="uq_account_full_init_generation"),
        Index(
            "ux_account_full_init_active",
            "account_id",
            unique=True,
            postgresql_where=text(ACTIVE_FULL_INIT_SQL),
            sqlite_where=text(ACTIVE_FULL_INIT_SQL),
        ),
        Index("ix_account_full_init_due", "status", "next_retry_at", "id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    account_id: Mapped[int] = mapped_column(ForeignKey("tg_accounts.id"))
    generation: Mapped[int] = mapped_column(Integer, default=1)
    policy_version: Mapped[str] = mapped_column(String(48), default="normal_full_init_v1")
    predecessor_initialization_id: Mapped[int | None] = mapped_column(
        ForeignKey("tg_account_full_initializations.id", ondelete="SET NULL"), nullable=True
    )
    target_pool_id: Mapped[int] = mapped_column(ForeignKey("account_pools.id"))
    profile_policy_version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(40), default="pending")
    stage: Mapped[str] = mapped_column(String(40), default="profile")
    authorization_generation: Mapped[int] = mapped_column(Integer, default=0)
    fixed_two_fa_version: Mapped[int] = mapped_column(Integer, default=0)
    source_two_fa_kind: Mapped[str] = mapped_column(String(40), default="unknown")
    source_two_fa_password_ciphertext: Mapped[str] = mapped_column(Text, default="")
    source_secret_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    two_fa_status: Mapped[str] = mapped_column(String(40), default="pending")
    two_fa_call_state: Mapped[str] = mapped_column(String(20), default="none")
    two_fa_request_key: Mapped[str] = mapped_column(String(100), default="")
    two_fa_evidence_ref: Mapped[str] = mapped_column(String(160), default="")
    profile_status: Mapped[str] = mapped_column(String(40), default="pending")
    profile_batch_id: Mapped[int | None] = mapped_column(
        ForeignKey("tg_account_security_batches.id", ondelete="SET NULL"), nullable=True
    )
    profile_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("tg_account_security_batch_items.id", ondelete="SET NULL"), nullable=True
    )
    profile_action_types: Mapped[str] = mapped_column(Text, default="[]")
    profile_target_name: Mapped[str] = mapped_column(String(120), default="")
    profile_target_avatar_source: Mapped[str] = mapped_column(String(300), default="")
    profile_target_avatar_object_key: Mapped[str] = mapped_column(String(500), default="")
    profile_evidence_ref: Mapped[str] = mapped_column(String(160), default="")
    abc_status: Mapped[str] = mapped_column(String(40), default="required")
    abc_batch_id: Mapped[str] = mapped_column(String(36), default="")
    abc_evidence_ref: Mapped[str] = mapped_column(String(160), default="")
    failure_type: Mapped[str] = mapped_column(String(100), default="")
    failure_detail: Mapped[str] = mapped_column(Text, default="")
    originating_actor: Mapped[str] = mapped_column(String(100), default="")
    execution_owner: Mapped[str] = mapped_column(String(100), default="account-post-login-init-worker")
    lease_token: Mapped[str] = mapped_column(String(80), default="")
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    two_fa_next_retry_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class TgAccountLoginPostInitializationBinding(Base):
    __tablename__ = "tg_account_login_post_init_bindings"
    __table_args__ = (
        UniqueConstraint(
            "login_item_id",
            "login_execution_generation",
            name="uq_login_post_init_binding_generation",
        ),
        Index("ix_login_post_init_binding_owner", "full_initialization_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    account_id: Mapped[int] = mapped_column(ForeignKey("tg_accounts.id"))
    login_item_id: Mapped[int] = mapped_column(ForeignKey("tg_account_login_batch_items.id"))
    login_execution_generation: Mapped[int] = mapped_column(Integer)
    full_initialization_id: Mapped[int] = mapped_column(ForeignKey("tg_account_full_initializations.id"))
    status: Mapped[str] = mapped_column(String(32), default="attached")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    detached_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class TgPostLoginAbcRequest(Base):
    __tablename__ = "tg_post_login_abc_requests"
    __table_args__ = (
        UniqueConstraint("full_initialization_id", name="uq_post_login_abc_full_init"),
        Index("ix_post_login_abc_request_status", "tenant_id", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    account_id: Mapped[int] = mapped_column(ForeignKey("tg_accounts.id"))
    full_initialization_id: Mapped[int] = mapped_column(ForeignKey("tg_account_full_initializations.id"))
    status: Mapped[str] = mapped_column(String(40), default="waiting_approval")
    request_version: Mapped[int] = mapped_column(Integer, default=1)
    requested_by: Mapped[str] = mapped_column(String(100), default="")
    approved_by: Mapped[str] = mapped_column(String(100), default="")
    approval_ref: Mapped[str] = mapped_column(String(160), default="")
    deployed_release_sha: Mapped[str] = mapped_column(String(64), default="")
    preview_fingerprint: Mapped[str] = mapped_column(String(64), default="")
    abc_batch_id: Mapped[str] = mapped_column(String(36), default="")
    failure_type: Mapped[str] = mapped_column(String(100), default="")
    failure_detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


__all__ = [
    "TERMINAL_FULL_INIT_STATUSES",
    "TgAccountFullInitialization",
    "TgAccountLoginPostInitializationBinding",
    "TgPostLoginAbcRequest",
]

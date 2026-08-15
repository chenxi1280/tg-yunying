from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, object_session

from app.database import Base

from .enums import now


class TgAccountLoginBatch(Base):
    __tablename__ = "tg_account_login_batches"
    __table_args__ = (
        UniqueConstraint("tenant_id", "recipient_user_id", "idempotency_key", name="uq_login_batch_idempotency"),
        Index("ix_login_batch_fair_claim", "status", "last_claimed_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    pool_id: Mapped[int] = mapped_column(ForeignKey("account_pools.id"))
    created_by: Mapped[str] = mapped_column(String(100))
    recipient_user_id: Mapped[int] = mapped_column(ForeignKey("app_users.id"))
    idempotency_key: Mapped[str] = mapped_column(String(80))
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(40), default="queued")
    state_version: Mapped[int] = mapped_column(Integer, default=1)
    execution_generation: Mapped[int] = mapped_column(Integer, default=1)
    resolution_version: Mapped[int] = mapped_column(Integer, default=0)
    total_count: Mapped[int] = mapped_column(Integer, default=0)
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    unresolved_count: Mapped[int] = mapped_column(Integer, default=0)
    warning_count: Mapped[int] = mapped_column(Integer, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, default=0)
    last_claimed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reason: Mapped[str] = mapped_column(String(255))
    trace_id: Mapped[str] = mapped_column(String(80))
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class TgAccountLoginBatchItem(Base):
    __tablename__ = "tg_account_login_batch_items"
    __table_args__ = (
        UniqueConstraint("batch_id", "line_no", name="uq_login_batch_item_line"),
        UniqueConstraint("batch_id", "phone_fingerprint", name="uq_login_batch_item_phone"),
        UniqueConstraint("batch_id", "code_source_uuid_fingerprint", name="uq_login_batch_item_uuid"),
        Index("ix_login_batch_item_due", "status", "next_retry_at", "batch_id", "line_no"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("tg_account_login_batches.id"))
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    line_no: Mapped[int] = mapped_column(Integer)
    phone_masked: Mapped[str] = mapped_column(String(60))
    phone_fingerprint: Mapped[str] = mapped_column(String(64))
    phone_fingerprint_version: Mapped[int] = mapped_column(Integer)
    phone_ciphertext: Mapped[str] = mapped_column(Text)
    code_url_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    credential_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    code_source_host: Mapped[str] = mapped_column(String(120))
    code_source_uuid_fingerprint: Mapped[str] = mapped_column(String(64))
    code_source_uuid_hint: Mapped[str] = mapped_column(String(40))
    replace_binding: Mapped[bool] = mapped_column(Boolean, default=False)
    expected_binding_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    route_hint: Mapped[str] = mapped_column(String(40))
    route: Mapped[str] = mapped_column(String(40), default="")
    account_id: Mapped[int | None] = mapped_column(ForeignKey("tg_accounts.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(40), default="pending")
    phase: Mapped[str] = mapped_column(String(40), default="prepare")
    failure_type: Mapped[str] = mapped_column(String(80), default="")
    failure_detail: Mapped[str] = mapped_column(Text, default="")
    warning_detail: Mapped[str] = mapped_column(Text, default="")
    current_attempt_id: Mapped[int | None] = mapped_column(ForeignKey("tg_account_login_batch_attempts.id"), nullable=True)
    execution_generation: Mapped[int] = mapped_column(Integer, default=1)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    state_version: Mapped[int] = mapped_column(Integer, default=1)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)

    @property
    def code_source_note(self) -> str:
        return f"{self.code_source_host} · {self.code_source_uuid_hint}"

    @property
    def current_attempt_state_version(self) -> int:
        session = object_session(self)
        attempt = session.get(TgAccountLoginBatchAttempt, self.current_attempt_id) if session and self.current_attempt_id else None
        return attempt.state_version if attempt else 0

    @property
    def reconcile_status(self) -> str:
        session = object_session(self)
        attempt = session.get(TgAccountLoginBatchAttempt, self.current_attempt_id) if session and self.current_attempt_id else None
        return attempt.reconcile_status if attempt else "none"

    @property
    def reconcile_attempted(self) -> bool:
        session = object_session(self)
        attempt = session.get(TgAccountLoginBatchAttempt, self.current_attempt_id) if session and self.current_attempt_id else None
        return bool(attempt and attempt.last_reconciled_at)

    @property
    def account_binding_version(self) -> int:
        from .accounts import TgAccount

        session = object_session(self)
        account = session.get(TgAccount, self.account_id) if session and self.account_id else None
        return account.code_source_binding_version if account else 0


class TgAccountLoginBatchAttempt(Base):
    __tablename__ = "tg_account_login_batch_attempts"
    __table_args__ = (
        UniqueConstraint("item_id", "execution_generation", name="uq_login_attempt_generation"),
        Index("ix_login_attempt_reconcile", "reconcile_status", "reconcile_until_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("tg_account_login_batch_items.id"))
    batch_id: Mapped[int] = mapped_column(ForeignKey("tg_account_login_batches.id"))
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    execution_generation: Mapped[int] = mapped_column(Integer)
    state_version: Mapped[int] = mapped_column(Integer, default=1)
    phase: Mapped[str] = mapped_column(String(40), default="prepare")
    lease_token: Mapped[str] = mapped_column(String(80), default="")
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    code_wait_until_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    flow_id: Mapped[int | None] = mapped_column(ForeignKey("tg_login_flows.id"), nullable=True)
    flow_version: Mapped[int] = mapped_column(Integer, default=0)
    baseline_code_hmac: Mapped[str] = mapped_column(String(64), default="")
    baseline_login_time_hmac: Mapped[str] = mapped_column(String(64), default="")
    send_request_seq: Mapped[int] = mapped_column(Integer, default=0)
    send_request_key: Mapped[str] = mapped_column(String(80), default="")
    send_call_state: Mapped[str] = mapped_column(String(20), default="none")
    code_verify_request_seq: Mapped[int] = mapped_column(Integer, default=0)
    code_verify_request_key: Mapped[str] = mapped_column(String(80), default="")
    code_verify_call_state: Mapped[str] = mapped_column(String(20), default="none")
    twofa_verify_request_seq: Mapped[int] = mapped_column(Integer, default=0)
    twofa_verify_request_key: Mapped[str] = mapped_column(String(80), default="")
    twofa_verify_call_state: Mapped[str] = mapped_column(String(20), default="none")
    reconcile_status: Mapped[str] = mapped_column(String(40), default="none")
    reconcile_until_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_reconciled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    authoritative_evidence_ref: Mapped[str] = mapped_column(String(160), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class TgAccountLoginBatchNotification(Base):
    __tablename__ = "tg_account_login_batch_notifications"
    __table_args__ = (
        UniqueConstraint(
            "batch_id", "execution_generation", "resolution_version", "channel", "recipient_user_id",
            name="uq_login_batch_notification_delivery",
        ),
        Index("ix_login_batch_notification_outbox", "channel", "delivery_status", "next_retry_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("tg_account_login_batches.id"))
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    execution_generation: Mapped[int] = mapped_column(Integer)
    resolution_version: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(20))
    channel: Mapped[str] = mapped_column(String(20))
    recipient_user_id: Mapped[int] = mapped_column(ForeignKey("app_users.id"))
    summary_json: Mapped[str] = mapped_column(Text)
    delivery_status: Mapped[str] = mapped_column(String(30), default="pending")
    delivery_attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    state_version: Mapped[int] = mapped_column(Integer, default=1)
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class TgAccountPhoneFingerprintAlias(Base):
    __tablename__ = "tg_account_phone_fingerprint_aliases"
    __table_args__ = (
        UniqueConstraint("tenant_id", "key_version", "fingerprint", name="uq_account_phone_alias_fingerprint"),
        Index("ix_account_phone_alias_account", "tenant_id", "account_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    account_id: Mapped[int] = mapped_column(ForeignKey("tg_accounts.id"))
    key_version: Mapped[int] = mapped_column(Integer)
    fingerprint: Mapped[str] = mapped_column(String(64))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class TgAccountLoginRateBucket(Base):
    __tablename__ = "tg_account_login_rate_buckets"
    __table_args__ = (
        UniqueConstraint("scope_type", "scope_id", name="uq_account_login_rate_bucket_scope"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scope_type: Mapped[str] = mapped_column(String(30))
    scope_id: Mapped[str] = mapped_column(String(120))
    next_available_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    active_leases: Mapped[int] = mapped_column(Integer, default=0)
    max_concurrency: Mapped[int] = mapped_column(Integer)
    lease_tokens_json: Mapped[str] = mapped_column(Text, default="[]")
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    state_version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


__all__ = [
    "TgAccountLoginBatch",
    "TgAccountLoginBatchAttempt",
    "TgAccountLoginBatchItem",
    "TgAccountLoginBatchNotification",
    "TgAccountLoginRateBucket",
    "TgAccountPhoneFingerprintAlias",
]

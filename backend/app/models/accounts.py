from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

from .enums import AccountStatus, DeveloperAppHealthStatus, now


class TelegramDeveloperApp(Base):
    __tablename__ = "telegram_developer_apps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    app_name: Mapped[str] = mapped_column(String(100))
    api_id: Mapped[int] = mapped_column(Integer, unique=True)
    api_hash_ciphertext: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    health_status: Mapped[str] = mapped_column(String(30), default=DeveloperAppHealthStatus.HEALTHY.value)
    max_accounts: Mapped[int] = mapped_column(Integer, default=0)
    credentials_version: Mapped[int] = mapped_column(Integer, default=1)
    last_assigned_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # noqa: F821
    last_check_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # noqa: F821
    last_error: Mapped[str] = mapped_column(Text, default="")
    notes: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)  # noqa: F821
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now)  # noqa: F821

    accounts: Mapped[list[TgAccount]] = relationship(back_populates="developer_app")  # noqa: F821


class TgAccount(Base):
    __tablename__ = "tg_accounts"
    __table_args__ = (UniqueConstraint("tenant_id", "phone_masked"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    pool_id: Mapped[int | None] = mapped_column(ForeignKey("account_pools.id"), nullable=True)
    display_name: Mapped[str] = mapped_column(String(120))
    username: Mapped[str | None] = mapped_column(String(120), nullable=True)
    tg_first_name: Mapped[str] = mapped_column(String(80), default="")
    tg_last_name: Mapped[str] = mapped_column(String(80), default="")
    tg_bio: Mapped[str] = mapped_column(Text, default="")
    avatar_object_key: Mapped[str] = mapped_column(String(300), default="")
    profile_sync_status: Mapped[str] = mapped_column(String(30), default="未同步")
    profile_sync_error: Mapped[str] = mapped_column(Text, default="")
    profile_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # noqa: F821
    phone_masked: Mapped[str] = mapped_column(String(60))
    phone_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    developer_app_id: Mapped[int | None] = mapped_column(ForeignKey("telegram_developer_apps.id"), nullable=True)
    developer_app_version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(30), default=AccountStatus.PENDING_LOGIN.value)
    health_score: Mapped[float] = mapped_column(Float, default=100)
    last_active_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # noqa: F821
    session_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)  # noqa: F821

    tenant: Mapped[Tenant] = relationship(back_populates="accounts")  # noqa: F821
    groups: Mapped[list[TgGroupAccount]] = relationship(back_populates="account")  # noqa: F821
    contacts: Mapped[list[TgContact]] = relationship(back_populates="account")  # noqa: F821
    developer_app: Mapped[TelegramDeveloperApp | None] = relationship(back_populates="accounts")
    pool: Mapped[AccountPool | None] = relationship(back_populates="accounts")  # noqa: F821

    @property
    def developer_app_name(self) -> str | None:
        return self.developer_app.app_name if self.developer_app else None

    @property
    def developer_api_id(self) -> int | None:
        return self.developer_app.api_id if self.developer_app else None

    @property
    def developer_app_health_status(self) -> str | None:
        return self.developer_app.health_status if self.developer_app else None

    @property
    def avatar_preview_url(self) -> str:
        return f"/media/{self.avatar_object_key}" if self.avatar_object_key else ""

    @property
    def pool_name(self) -> str:
        return self.pool.name if self.pool else "默认账号池"


class TgAccountProfileSyncRecord(Base):
    __tablename__ = "tg_account_profile_sync_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    account_id: Mapped[int] = mapped_column(ForeignKey("tg_accounts.id"))
    actor: Mapped[str] = mapped_column(String(100), default="")
    before_snapshot: Mapped[str] = mapped_column(Text, default="")
    after_snapshot: Mapped[str] = mapped_column(Text, default="")
    avatar_object_key: Mapped[str] = mapped_column(String(300), default="")
    status: Mapped[str] = mapped_column(String(30), default="排队中")
    failure_type: Mapped[str] = mapped_column(String(40), default="")
    failure_detail: Mapped[str] = mapped_column(Text, default="")
    remote_detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)  # noqa: F821
    synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # noqa: F821


class TgAccountSyncRecord(Base):
    __tablename__ = "tg_account_sync_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    account_id: Mapped[int] = mapped_column(ForeignKey("tg_accounts.id"))
    sync_type: Mapped[str] = mapped_column(String(40))
    trigger_source: Mapped[str] = mapped_column(String(60), default="manual")
    status: Mapped[str] = mapped_column(String(30), default="排队中")
    result_count: Mapped[int] = mapped_column(Integer, default=0)
    failure_type: Mapped[str] = mapped_column(String(40), default="")
    failure_detail: Mapped[str] = mapped_column(Text, default="")
    scheduled_at: Mapped[datetime] = mapped_column(DateTime, default=now)  # noqa: F821
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # noqa: F821
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # noqa: F821
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)  # noqa: F821


class TgContact(Base):
    __tablename__ = "tg_contacts"
    __table_args__ = (UniqueConstraint("account_id", "peer_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    account_id: Mapped[int] = mapped_column(ForeignKey("tg_accounts.id"))
    peer_id: Mapped[str] = mapped_column(String(120))
    display_name: Mapped[str] = mapped_column(String(160))
    username: Mapped[str | None] = mapped_column(String(120), nullable=True)
    phone_masked: Mapped[str] = mapped_column(String(60), default="")
    contact_type: Mapped[str] = mapped_column(String(40), default="private")
    is_mutual: Mapped[bool] = mapped_column(Boolean, default=False)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # noqa: F821
    last_synced_at: Mapped[datetime] = mapped_column(DateTime, default=now)  # noqa: F821
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)  # noqa: F821

    account: Mapped[TgAccount] = relationship(back_populates="contacts")


class TgLoginFlow(Base):
    __tablename__ = "tg_login_flows"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    account_id: Mapped[int] = mapped_column(ForeignKey("tg_accounts.id"))
    method: Mapped[str] = mapped_column(String(24))
    status: Mapped[str] = mapped_column(String(30))
    code_preview: Mapped[str | None] = mapped_column(String(16), nullable=True)
    code_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # noqa: F821
    qr_payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)  # noqa: F821


class TgVerificationCode(Base):
    __tablename__ = "tg_verification_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    account_id: Mapped[int] = mapped_column(ForeignKey("tg_accounts.id"))
    source: Mapped[str] = mapped_column(String(40), default="telegram_service_message")
    code_preview: Mapped[str | None] = mapped_column(String(24), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # noqa: F821
    viewed_by: Mapped[str] = mapped_column(String(100), default="")
    viewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # noqa: F821
    status: Mapped[str] = mapped_column(String(30), default="可查看")
    raw_hint: Mapped[str] = mapped_column(String(160), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)  # noqa: F821


__all__ = [
    "TgAccount",
    "TgAccountProfileSyncRecord",
    "TgAccountSyncRecord",
    "TgContact",
    "TgLoginFlow",
    "TgVerificationCode",
    "TelegramDeveloperApp",
]

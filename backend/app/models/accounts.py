from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.security import decrypt_secret

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
    last_assigned_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_check_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    notes: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    accounts: Mapped[list[TgAccount]] = relationship(back_populates="developer_app")


class TgAccount(Base):
    __tablename__ = "tg_accounts"

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
    profile_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    phone_masked: Mapped[str] = mapped_column(String(60))
    phone_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    developer_app_id: Mapped[int | None] = mapped_column(ForeignKey("telegram_developer_apps.id"), nullable=True)
    proxy_id: Mapped[int | None] = mapped_column(ForeignKey("account_proxies.id"), nullable=True)
    developer_app_version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(30), default=AccountStatus.PENDING_LOGIN.value)
    health_score: Mapped[float] = mapped_column(Float, default=100)
    last_active_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    session_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    deleted_by: Mapped[str] = mapped_column(String(100), default="")
    delete_reason: Mapped[str] = mapped_column(String(255), default="")

    tenant: Mapped[Tenant] = relationship(back_populates="accounts")
    groups: Mapped[list[TgGroupAccount]] = relationship(back_populates="account")
    contacts: Mapped[list[TgContact]] = relationship(back_populates="account")
    developer_app: Mapped[TelegramDeveloperApp | None] = relationship(back_populates="accounts")
    proxy: Mapped[AccountProxy | None] = relationship(back_populates="accounts")
    pool: Mapped[AccountPool | None] = relationship(back_populates="accounts")

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
    def proxy_name(self) -> str | None:
        return self.proxy.name if self.proxy else None

    @property
    def proxy_local_address(self) -> str | None:
        return self.proxy.local_address if self.proxy else None

    @property
    def proxy_status(self) -> str | None:
        return self.proxy.status if self.proxy else None

    @property
    def proxy_alert_status(self) -> str | None:
        return self.proxy.alert_status if self.proxy else None

    @property
    def avatar_preview_url(self) -> str:
        return f"/media/{self.avatar_object_key}" if self.avatar_object_key else ""

    @property
    def phone_number(self) -> str | None:
        return decrypt_secret(self.phone_ciphertext) if self.phone_ciphertext else self.phone_masked

    @property
    def pool_name(self) -> str:
        return self.pool.name if self.pool else "默认账号池"


Index(
    "ux_tg_accounts_tenant_phone_active",
    TgAccount.tenant_id,
    TgAccount.phone_masked,
    unique=True,
    postgresql_where=TgAccount.deleted_at.is_(None),
    sqlite_where=TgAccount.deleted_at.is_(None),
)


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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


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
    scheduled_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


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
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_synced_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    account: Mapped[TgAccount] = relationship(back_populates="contacts")


class TgLoginFlow(Base):
    __tablename__ = "tg_login_flows"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    account_id: Mapped[int] = mapped_column(ForeignKey("tg_accounts.id"))
    method: Mapped[str] = mapped_column(String(24))
    status: Mapped[str] = mapped_column(String(30))
    code_preview: Mapped[str | None] = mapped_column(String(16), nullable=True)
    code_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    qr_payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class TgVerificationCode(Base):
    __tablename__ = "tg_verification_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    account_id: Mapped[int] = mapped_column(ForeignKey("tg_accounts.id"))
    source: Mapped[str] = mapped_column(String(40), default="telegram_service_message")
    code_preview: Mapped[str | None] = mapped_column(String(24), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    viewed_by: Mapped[str] = mapped_column(String(100), default="")
    viewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="可查看")
    raw_hint: Mapped[str] = mapped_column(String(160), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


__all__ = [
    "TgAccount",
    "TgAccountProfileSyncRecord",
    "TgAccountSyncRecord",
    "TgContact",
    "TgLoginFlow",
    "TgVerificationCode",
    "TelegramDeveloperApp",
]

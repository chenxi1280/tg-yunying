from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.security import decrypt_secret

from .enums import now


class GroupArchive(Base):
    __tablename__ = "group_archives"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    group_id: Mapped[int] = mapped_column(ForeignKey("tg_groups.id"))
    collection_account_id: Mapped[int | None] = mapped_column(ForeignKey("tg_accounts.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(160))
    status: Mapped[str] = mapped_column(String(30), default="已完成")
    sync_mode: Mapped[str] = mapped_column(String(30), default="sync")
    failure_detail: Mapped[str] = mapped_column(Text, default="")
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    member_count: Mapped[int] = mapped_column(Integer, default=0)
    summary: Mapped[str] = mapped_column(Text, default="")
    new_group_plan: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class ArchivedMessage(Base):
    __tablename__ = "archived_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    archive_id: Mapped[int] = mapped_column(ForeignKey("group_archives.id"))
    sender_peer_id: Mapped[str] = mapped_column(String(120), default="")
    remote_message_id: Mapped[str] = mapped_column(String(160), default="")
    sender_name: Mapped[str] = mapped_column(String(120))
    sender_phone_masked: Mapped[str] = mapped_column(String(60), default="")
    sender_phone_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    content: Mapped[str] = mapped_column(Text)
    message_type: Mapped[str] = mapped_column(String(40), default="text")
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    @property
    def sender_phone_number(self) -> str | None:
        return decrypt_secret(self.sender_phone_ciphertext) if self.sender_phone_ciphertext else self.sender_phone_masked


class ArchivedMember(Base):
    __tablename__ = "archived_members"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    archive_id: Mapped[int] = mapped_column(ForeignKey("group_archives.id"))
    peer_id: Mapped[str] = mapped_column(String(120), default="")
    display_name: Mapped[str] = mapped_column(String(120))
    username: Mapped[str | None] = mapped_column(String(120), nullable=True)
    phone_masked: Mapped[str] = mapped_column(String(60), default="")
    phone_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    activity_score: Mapped[int] = mapped_column(Integer, default=0)
    tags: Mapped[str] = mapped_column(String(160), default="")
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    @property
    def phone_number(self) -> str | None:
        return decrypt_secret(self.phone_ciphertext) if self.phone_ciphertext else self.phone_masked


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int | None] = mapped_column(ForeignKey("tenants.id"), nullable=True)
    actor: Mapped[str] = mapped_column(String(100))
    action: Mapped[str] = mapped_column(String(100))
    target_type: Mapped[str] = mapped_column(String(80))
    target_id: Mapped[str] = mapped_column(String(80))
    detail: Mapped[str] = mapped_column(Text, default="")
    ip_address: Mapped[str] = mapped_column(String(80), default="127.0.0.1")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


__all__ = ["GroupArchive", "ArchivedMessage", "ArchivedMember", "AuditLog"]

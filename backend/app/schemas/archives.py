from __future__ import annotations
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from .api import ApiModel


class ArchiveCreate(BaseModel):
    tenant_id: int = 1
    group_id: int | None = None
    operation_target_id: int | None = None
    title: str


class ArchiveExportRequest(BaseModel):
    export_format: str = "json"


class ArchiveOut(ApiModel):
    id: int
    tenant_id: int
    group_id: int
    collection_account_id: int | None = None
    title: str
    status: str
    sync_mode: str
    failure_detail: str
    message_count: int
    member_count: int
    summary: str
    new_group_plan: str
    started_at: datetime | None = None
    finished_at: datetime | None = None
    last_synced_at: datetime | None = None
    created_at: datetime


class ArchivedMessageOut(ApiModel):
    id: int
    archive_id: int
    sender_peer_id: str = ""
    remote_message_id: str = ""
    sender_name: str
    sender_phone_masked: str = ""
    sender_phone_number: str | None = None
    content: str
    message_type: str
    sent_at: datetime


class ArchivedMemberOut(ApiModel):
    id: int
    archive_id: int
    peer_id: str = ""
    display_name: str
    username: str | None
    phone_masked: str = ""
    phone_number: str | None = None
    activity_score: int
    tags: str
    last_seen_at: datetime | None = None


class ArchiveDetailOut(BaseModel):
    archive: ArchiveOut
    messages: list[ArchivedMessageOut]
    members: list[ArchivedMemberOut]
    invite_candidates: list[ArchivedMemberOut] = []


class ArchiveExportOut(BaseModel):
    archive: ArchiveOut
    export_format: str
    generated_at: datetime
    message_count: int
    member_count: int
    messages: list[ArchivedMessageOut]
    members: list[ArchivedMemberOut]
    invite_candidates: list[ArchivedMemberOut] = []


# ── Audit ──

class AuditLogOut(ApiModel):
    id: int
    tenant_id: int | None
    actor: str
    action: str
    target_type: str
    target_id: str
    detail: str
    account_display_name: str | None = None
    account_phone_number: str | None = None
    ip_address: str
    created_at: datetime


# ── Reports / Overview ──

class OverviewOut(BaseModel):
    totals: dict[str, int]
    rates: dict[str, float]
    queue: dict[str, int]
    risks: list[dict[str, Any]]
    activity_24h: list[dict[str, Any]] = Field(default_factory=list)
    operation_center: dict[str, Any] | None = None


class ReportOut(BaseModel):
    accounts: dict[str, Any]
    groups: dict[str, Any]
    tasks: dict[str, Any]
    tenant: dict[str, Any]


__all__ = [
    "ArchiveCreate", "ArchiveOut", "ArchivedMessageOut", "ArchivedMemberOut",
    "ArchiveDetailOut", "ArchiveExportRequest", "ArchiveExportOut",
    "AuditLogOut", "OverviewOut", "ReportOut",
]

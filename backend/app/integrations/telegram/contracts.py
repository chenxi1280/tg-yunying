from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class LoginChallenge:
    status: str
    code_preview: str | None = None
    code_expires_at: datetime | None = None
    qr_payload: str | None = None


@dataclass(frozen=True)
class SendResult:
    ok: bool
    remote_message_id: str | None = None
    failure_type: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class CachedMediaResult:
    ok: bool
    data: bytes = b""
    failure_type: str = ""
    detail: str = ""


@dataclass(frozen=True)
class OutboundSegment:
    segment_type: str
    content: str = ""
    source: str | None = None
    caption: str = ""


@dataclass(frozen=True)
class ProfileUpdateResult:
    ok: bool
    detail: str = ""
    failure_type: str | None = None


@dataclass(frozen=True)
class OperationResult:
    ok: bool
    status: str = "已完成"
    failure_type: str = ""
    detail: str = ""


@dataclass(frozen=True)
class ChannelMembershipResult(OperationResult):
    membership_status: str = "joined"


@dataclass(frozen=True)
class GroupSnapshot:
    tg_peer_id: str
    title: str
    group_type: str
    member_count: int
    permission_label: str
    can_send: bool
    slowmode_seconds: int | None = None
    username: str | None = None


@dataclass(frozen=True)
class AccountHealth:
    status: str
    health_score: float
    detail: str


@dataclass(frozen=True)
class VerificationCodeSnapshot:
    code: str
    raw_hint: str
    expires_at: datetime | None


@dataclass(frozen=True)
class ContactSnapshot:
    peer_id: str
    display_name: str
    username: str | None = None
    phone: str | None = None
    contact_type: str = "private"
    is_mutual: bool = False
    last_message_at: datetime | None = None


@dataclass(frozen=True)
class RemoteProfile:
    first_name: str
    last_name: str
    bio: str
    username: str | None = None


@dataclass(frozen=True)
class AccountAuthorizationSnapshot:
    authorization_hash: str
    is_current: bool
    device_model: str
    platform: str
    system_version: str
    api_id: int
    app_name: str
    app_version: str
    ip: str = ""
    country: str = ""
    region: str = ""
    date_created: datetime | None = None
    date_active: datetime | None = None


@dataclass(frozen=True)
class AccountSecurityOperationResult:
    ok: bool
    status: str = "已完成"
    failure_type: str = ""
    detail: str = ""
    next_retry_at: datetime | None = None


@dataclass(frozen=True)
class ArchivedMessageSnapshot:
    sender_name: str
    content: str
    message_type: str = "text"
    sent_at: datetime | None = None
    is_bot: bool = False
    sender_phone: str | None = None


@dataclass(frozen=True)
class GroupMessageSnapshot:
    remote_message_id: str
    sender_name: str
    content: str
    sender_peer_id: str = ""
    sender_username: str = ""
    sender_peer_type: str = ""
    message_type: str = "text"
    sent_at: datetime | None = None
    is_bot: bool = False
    sender_role: str = "member"
    caption: str = ""
    media_type: str = ""
    media_fingerprint: str = ""
    media_group_id: str = ""
    media_group_index: int = 0
    media_group_total: int = 1


@dataclass(frozen=True)
class ChannelMessageSnapshot:
    message_id: int
    content_preview: str = ""
    message_url: str = ""
    published_at: datetime | None = None


@dataclass(frozen=True)
class ChannelCommentSnapshot:
    comment_message_id: int
    parent_comment_message_id: int | None = None
    author_peer_id: str = ""
    author_username: str = ""
    author_name: str = ""
    content_preview: str = ""
    reply_count: int = 0
    published_at: datetime | None = None
    is_bot: bool = False


@dataclass(frozen=True)
class ArchivedMemberSnapshot:
    display_name: str
    username: str | None = None
    phone: str | None = None
    activity_score: int = 0
    tags: str = ""


@dataclass(frozen=True)
class ArchiveSnapshot:
    messages: list[ArchivedMessageSnapshot]
    members: list[ArchivedMemberSnapshot]
    summary: str
    new_group_plan: str


@dataclass(frozen=True)
class DeveloperAppCredentials:
    app_id: int | None
    api_id: int
    api_hash: str
    credentials_version: int
    app_name: str = ""

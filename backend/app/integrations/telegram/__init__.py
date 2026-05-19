"""Telegram gateway adapter package."""

from .contracts import (
    AccountHealth,
    ArchiveSnapshot,
    ArchivedMemberSnapshot,
    ArchivedMessageSnapshot,
    ChannelMembershipResult,
    ChannelCommentSnapshot,
    ChannelMessageSnapshot,
    ContactSnapshot,
    DeveloperAppCredentials,
    GroupMessageSnapshot,
    GroupSnapshot,
    LoginChallenge,
    OperationResult,
    OutboundSegment,
    ProfileUpdateResult,
    RemoteProfile,
    SendResult,
    VerificationCodeSnapshot,
)
from .gateway import TelethonTelegramGateway, create_gateway
from .gateway import _resolve_telethon_target, _telethon_send_target
from .mock import TelegramGateway

__all__ = [
    "AccountHealth",
    "ArchiveSnapshot",
    "ArchivedMemberSnapshot",
    "ArchivedMessageSnapshot",
    "ChannelCommentSnapshot",
    "ChannelMessageSnapshot",
    "ChannelMembershipResult",
    "ContactSnapshot",
    "DeveloperAppCredentials",
    "GroupMessageSnapshot",
    "GroupSnapshot",
    "LoginChallenge",
    "OperationResult",
    "OutboundSegment",
    "ProfileUpdateResult",
    "RemoteProfile",
    "SendResult",
    "TelethonTelegramGateway",
    "TelegramGateway",
    "VerificationCodeSnapshot",
    "create_gateway",
    "_resolve_telethon_target",
    "_telethon_send_target",
]

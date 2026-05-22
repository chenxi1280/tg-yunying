from __future__ import annotations

import tempfile
from uuid import uuid4
from typing import Callable

from .contracts import (
    ArchiveSnapshot,
    ArchivedMemberSnapshot,
    ArchivedMessageSnapshot,
    ChannelCommentSnapshot,
    ChannelMessageSnapshot,
    GroupMessageSnapshot,
    SendResult,
)
from .mock import source_media_hint
from .telethon_utils import resolve_telethon_target


async def _sender_role(client, target, sender) -> str:
    if sender is None:
        return "unknown"
    try:
        permissions = await client.get_permissions(target, sender)
    except Exception:
        return "unknown"
    participant = getattr(permissions, "participant", None)
    names = {type(permissions).__name__.lower(), type(participant).__name__.lower() if participant is not None else ""}
    if getattr(permissions, "is_creator", False) or any("creator" in name for name in names):
        return "owner"
    if getattr(permissions, "is_admin", False) or any("admin" in name for name in names):
        return "admin"
    return "member"


async def fetch_group_archive(client, peer_id: str) -> ArchiveSnapshot:
    target = await resolve_telethon_target(client, peer_id, group_id=1)
    messages_resp = await client.get_messages(target, limit=50)
    messages: list[ArchivedMessageSnapshot] = []
    for message in list(messages_resp or []):
        text = getattr(message, "message", "") or ""
        if not text and not getattr(message, "media", None):
            continue
        sender = await message.get_sender() if hasattr(message, "get_sender") else None
        sender_name = (
            getattr(sender, "first_name", "") or getattr(sender, "title", "") or getattr(sender, "username", None) or "未知成员"
        )
        messages.append(
            ArchivedMessageSnapshot(
                sender_name=sender_name,
                sender_phone=getattr(sender, "phone", None),
                content=text or "[media]",
                message_type="media" if getattr(message, "media", None) else "text",
                sent_at=getattr(message, "date", None),
                is_bot=bool(getattr(sender, "bot", False)),
            )
        )
    participants: list[ArchivedMemberSnapshot] = []
    counts: dict[str, int] = {}
    for item in messages:
        counts[item.sender_name] = counts.get(item.sender_name, 0) + 1
    async for participant in client.iter_participants(target, limit=80):
        name = f"{getattr(participant, 'first_name', '')} {getattr(participant, 'last_name', '')}".strip() or getattr(participant, "username", None) or str(getattr(participant, "id", ""))
        activity = min(100, counts.get(name, 0) * 20 + (20 if getattr(participant, "username", None) else 0))
        tags = "可邀请" if activity >= 40 else "观察"
        participants.append(
            ArchivedMemberSnapshot(
                display_name=name,
                username=getattr(participant, "username", None),
                phone=getattr(participant, "phone", None),
                activity_score=activity,
                tags=tags,
            )
        )
    participants.sort(key=lambda item: item.activity_score, reverse=True)
    return ArchiveSnapshot(
        messages=messages[:50],
        members=participants[:80],
        summary="群内近期讨论已归档，可继续提炼欢迎语、FAQ 和拉新邀请名单。",
        new_group_plan="新群建议延续原讨论主题，先铺欢迎语和 FAQ，再召回高活跃成员种子。",
    )


async def fetch_group_messages(client, peer_id: str, limit: int) -> list[GroupMessageSnapshot]:
    target = await resolve_telethon_target(client, peer_id, group_id=1)
    messages_resp = await client.get_messages(target, limit=limit)
    grouped_totals: dict[str, int] = {}
    grouped_seen: dict[str, int] = {}
    for message in list(messages_resp or []):
        group_id = str(getattr(message, "grouped_id", "") or "")
        if group_id:
            grouped_totals[group_id] = grouped_totals.get(group_id, 0) + 1
    snapshots: list[GroupMessageSnapshot] = []
    sender_role_cache: dict[str, str] = {}
    for message in list(messages_resp or []):
        text = getattr(message, "message", "") or ""
        if not text and not getattr(message, "media", None):
            continue
        sender = await message.get_sender() if hasattr(message, "get_sender") else None
        sender_peer_id = str(getattr(sender, "id", "") or "")
        sender_username = str(getattr(sender, "username", "") or "")
        sender_name = (
            getattr(sender, "first_name", "")
            or getattr(sender, "title", "")
            or getattr(sender, "username", None)
            or sender_peer_id
            or "未知成员"
        )
        role_cache_key = sender_peer_id or f"anonymous:{sender_name}"
        if role_cache_key not in sender_role_cache:
            sender_role_cache[role_cache_key] = await _sender_role(client, target, sender)
        sender_role = sender_role_cache[role_cache_key]
        group_id = str(getattr(message, "grouped_id", "") or "")
        if group_id:
            grouped_seen[group_id] = grouped_seen.get(group_id, 0) + 1
        media = getattr(message, "media", None)
        media_type = type(media).__name__ if media else ""
        remote_id = str(getattr(message, "id", uuid4().hex))
        snapshots.append(
            GroupMessageSnapshot(
                remote_message_id=remote_id,
                sender_peer_id=sender_peer_id,
                sender_name=sender_name,
                sender_username=sender_username,
                content=text or "[media]",
                message_type="media" if media else "text",
                sent_at=getattr(message, "date", None),
                is_bot=bool(getattr(sender, "bot", False)),
                sender_role=sender_role,
                caption=text,
                media_type=media_type,
                media_fingerprint=source_media_hint(peer_id, remote_id, group_id, media_type),
                media_group_id=group_id,
                media_group_index=grouped_seen.get(group_id, 0) if group_id else 0,
                media_group_total=grouped_totals.get(group_id, 1) if group_id else 1,
            )
        )
    return snapshots


async def cache_source_media(client, source_peer_id: str, source_message_id: str, cache_peer_id: str, map_send_error: Callable[[Exception], SendResult]) -> SendResult:
    try:
        source = await resolve_telethon_target(client, source_peer_id, group_id=0)
        cache_target = await resolve_telethon_target(client, cache_peer_id, group_id=0)
        source_message = await client.get_messages(source, ids=int(source_message_id))
        if not source_message or not getattr(source_message, "media", None):
            return SendResult(False, failure_type="source_media_unrecoverable", detail="源媒体消息不存在或无媒体")
        with tempfile.TemporaryDirectory(prefix="tg-source-media-cache-") as temp_dir:
            downloaded = await client.download_media(source_message, file=temp_dir)
            if not downloaded:
                return SendResult(False, failure_type="source_media_cache_failed", detail="源媒体下载失败")
            cached = await client.send_file(cache_target, downloaded, caption=getattr(source_message, "message", "") or None)
        return SendResult(True, remote_message_id=str(getattr(cached, "id", "")))
    except Exception as exc:
        return map_send_error(exc)


async def cache_material_source(client, source: str, cache_peer_id: str, caption: str, map_send_error: Callable[[Exception], SendResult]) -> SendResult:
    try:
        cache_target = await resolve_telethon_target(client, cache_peer_id, group_id=0)
        cached = await client.send_file(cache_target, source, caption=caption or None)
        return SendResult(True, remote_message_id=str(getattr(cached, "id", "")))
    except Exception as exc:
        return map_send_error(exc)


async def fetch_channel_messages(client, channel_peer_id: str, limit: int) -> list[ChannelMessageSnapshot]:
    target: int | str = int(channel_peer_id) if channel_peer_id.lstrip("-").isdigit() else channel_peer_id
    entity = await client.get_entity(target)
    messages_resp = await client.get_messages(entity, limit=limit)
    snapshots: list[ChannelMessageSnapshot] = []
    username = getattr(entity, "username", None)
    for message in list(messages_resp or []):
        message_id = int(getattr(message, "id", 0) or 0)
        if message_id <= 0:
            continue
        text = (getattr(message, "message", "") or "").strip()
        message_url = f"https://t.me/{username}/{message_id}" if username else ""
        snapshots.append(
            ChannelMessageSnapshot(
                message_id=message_id,
                content_preview=text[:500],
                message_url=message_url,
                published_at=getattr(message, "date", None),
            )
        )
    return snapshots


async def fetch_channel_comments(client, channel_peer_id: str, message_id: int, limit: int) -> list[ChannelCommentSnapshot]:
    target: int | str = int(channel_peer_id) if channel_peer_id.lstrip("-").isdigit() else channel_peer_id
    entity = await client.get_entity(target)
    snapshots: list[ChannelCommentSnapshot] = []
    async for comment in client.iter_messages(entity, reply_to=message_id, limit=limit):
        comment_id = int(getattr(comment, "id", 0) or 0)
        if comment_id <= 0:
            continue
        sender = getattr(comment, "sender", None)
        author_name = " ".join(
            item for item in [getattr(sender, "first_name", "") or "", getattr(sender, "last_name", "") or ""] if item
        ).strip() or getattr(sender, "title", "") or getattr(sender, "username", "") or ""
        replies = getattr(comment, "replies", None)
        snapshots.append(
            ChannelCommentSnapshot(
                comment_message_id=comment_id,
                parent_comment_message_id=getattr(getattr(comment, "reply_to", None), "reply_to_msg_id", None),
                author_peer_id=str(getattr(sender, "id", "") or ""),
                author_name=author_name,
                content_preview=(getattr(comment, "message", "") or "").strip()[:500],
                reply_count=int(getattr(replies, "replies", 0) or 0),
                published_at=getattr(comment, "date", None),
            )
        )
    return snapshots

from __future__ import annotations

import tempfile
from typing import Any
from urllib.parse import unquote, urlparse

from .gateway_contracts import OutboundSegment
from .gateway_telethon_utils import resolve_telethon_target


async def send_media_segment(client: Any, target: Any, segment: OutboundSegment) -> Any:
    source = segment.source or segment.content
    if not source:
        raise ValueError("媒体素材缺少可发送来源")
    custom_emoji = _parse_custom_emoji_source(source)
    if custom_emoji:
        document_id, alt = custom_emoji
        return await _send_custom_emoji_segment(client, target, document_id, alt, segment.caption or segment.content or "")
    caption = segment.caption or segment.content or None
    cache_ref = _parse_tg_cache_source(source)
    if not cache_ref:
        return await client.send_file(target, source, caption=caption)
    cache_peer, message_id = cache_ref
    cache_target = await resolve_telethon_target(client, cache_peer, group_id=0)
    cached_message = await client.get_messages(cache_target, ids=message_id)
    if not cached_message or not getattr(cached_message, "media", None):
        raise ValueError("TG 缓存消息不可下载")
    with tempfile.TemporaryDirectory(prefix="tg-material-reupload-") as temp_dir:
        downloaded = await client.download_media(cached_message, file=temp_dir)
        if not downloaded:
            raise ValueError("TG 缓存媒体下载失败")
        return await client.send_file(target, downloaded, caption=caption)


def _parse_tg_cache_source(source: str) -> tuple[str, int] | None:
    parsed = urlparse(source)
    if parsed.scheme != "tg-cache":
        return None
    peer = unquote(parsed.netloc).strip()
    message_id = parsed.path.strip("/").split("/", 1)[0]
    if not peer or not message_id.isdigit():
        raise ValueError("TG 缓存引用格式无效")
    return peer, int(message_id)


def _parse_custom_emoji_source(source: str) -> tuple[int, str] | None:
    if not source.startswith("custom_emoji:"):
        return None
    parts = source.split(":", 2)
    if len(parts) != 3 or not parts[1].isdigit() or not parts[2].strip():
        raise ValueError("custom emoji 素材格式无效")
    return int(parts[1]), parts[2].strip()


async def _send_custom_emoji_segment(client: Any, target: Any, document_id: int, alt: str, caption: str) -> Any:
    from telethon import types

    prefix = f"{caption}\n" if caption else ""
    text = f"{prefix}{alt}"
    entity = types.MessageEntityCustomEmoji(
        offset=_telegram_entity_length(prefix),
        length=_telegram_entity_length(alt),
        document_id=document_id,
    )
    try:
        return await client.send_message(target, text, formatting_entities=[entity])
    except TypeError:
        return await client.send_message(target, text, entities=[entity])


def _telegram_entity_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2

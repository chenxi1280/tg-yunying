from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from .contracts import OutboundSegment
from .telethon_media import send_media_segment


TYPING_DELAY_MIN_SECONDS = 1.0
TYPING_DELAY_MAX_SECONDS = 2.5
TYPING_DELAY_SECONDS_PER_CHARACTER = 0.05


@dataclass
class SendProgress:
    remote_message_id: str | None = None
    send_call_started: bool = False

    def mark_started(self) -> None:
        self.send_call_started = True

    def record_message(self, message: Any) -> None:
        fallback = self.remote_message_id or uuid4().hex[:8]
        self.remote_message_id = str(getattr(message, "id", fallback))


async def send_content(
    client: Any,
    target: Any,
    *,
    content: str,
    segments: list[OutboundSegment] | None,
    reply_to_message_id: int | None,
    progress: SendProgress,
) -> None:
    if not segments:
        message = await _send_text_message(
            client,
            target,
            content=content,
            reply_to_message_id=reply_to_message_id,
            progress=progress,
        )
        progress.record_message(message)
        return
    for segment in segments:
        message = await _send_segment(
            client,
            target,
            segment=segment,
            reply_to_message_id=reply_to_message_id,
            progress=progress,
        )
        progress.record_message(message)


async def _send_text_message(
    client: Any,
    target: Any,
    *,
    content: str,
    reply_to_message_id: int | None,
    progress: SendProgress,
):
    await send_typing_action(client, target, content)
    progress.mark_started()
    return await client.send_message(
        target,
        content,
        reply_to=reply_to_message_id,
        link_preview=False,
    )


async def send_typing_action(client: Any, target: Any, content: str) -> None:
    from telethon import functions, types

    await client(
        functions.messages.SetTypingRequest(
            peer=target,
            action=types.SendMessageTypingAction(),
        )
    )
    delay = len(content or "") * TYPING_DELAY_SECONDS_PER_CHARACTER
    await asyncio.sleep(
        min(TYPING_DELAY_MAX_SECONDS, max(TYPING_DELAY_MIN_SECONDS, delay))
    )


async def _send_segment(
    client: Any,
    target: Any,
    *,
    segment: OutboundSegment,
    reply_to_message_id: int | None,
    progress: SendProgress,
):
    if segment.segment_type == "文本":
        return await _send_text_message(
            client,
            target,
            content=segment.content or "",
            reply_to_message_id=reply_to_message_id,
            progress=progress,
        )
    if segment.segment_type == "链接":
        text = "\n".join(
            piece for piece in [segment.content, segment.source] if piece
        ).strip()
        progress.mark_started()
        return await client.send_message(
            target,
            text,
            reply_to=reply_to_message_id,
            link_preview=False,
        )
    return await send_media_segment(
        client,
        target,
        segment,
        reply_to_message_id=reply_to_message_id,
        before_send=progress.mark_started,
    )


__all__ = ["SendProgress", "send_content", "send_typing_action"]

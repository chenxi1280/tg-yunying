from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Protocol


ADMIN_CHAT_ID_MAX_LENGTH = 1000


class ChatMessageResult(Protocol):
    ok: bool
    detail: str


@dataclass(frozen=True)
class AdminChatBroadcastSummary:
    ok: bool
    detail: str


def parse_admin_chat_ids(raw_admin_chat_id: str | None) -> list[str]:
    values = re.split(r"[\s,，;；]+", str(raw_admin_chat_id or "").strip())
    unique_chat_ids: list[str] = []
    seen: set[str] = set()
    for value in values:
        chat_id = value.strip()
        if not chat_id or chat_id in seen:
            continue
        unique_chat_ids.append(chat_id)
        seen.add(chat_id)
    return unique_chat_ids


def admin_chat_is_allowed(raw_admin_chat_id: str | None, chat_id: str) -> bool:
    return str(chat_id).strip() in set(parse_admin_chat_ids(raw_admin_chat_id))


def send_admin_chat_broadcast(
    *,
    bot_token: str,
    raw_admin_chat_id: str,
    text: str,
    sender: Callable[[str, str, str], ChatMessageResult],
) -> AdminChatBroadcastSummary:
    chat_ids = parse_admin_chat_ids(raw_admin_chat_id)
    if not chat_ids:
        return AdminChatBroadcastSummary(False, "Telegram Bot admin chat id not configured")
    failures: list[str] = []
    for chat_id in chat_ids:
        result = sender(bot_token, chat_id, text)
        if not result.ok:
            failures.append(f"{chat_id}: {result.detail}")
    if failures:
        return AdminChatBroadcastSummary(False, "; ".join(failures))
    return AdminChatBroadcastSummary(True, f"sent:{len(chat_ids)}")

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import urlparse

from telethon import functions


TELEGRAM_HOSTS = frozenset(
    {"t.me", "telegram.me", "www.t.me", "www.telegram.me"}
)
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_]{5,32}$")


@dataclass(frozen=True)
class SearchResultEntityLink:
    position: int
    url: str
    username: str


@dataclass(frozen=True)
class TargetOpenEvidence:
    entity_id: str
    title: str
    username: str
    rpc: str = "channels.GetFullChannelRequest"


def search_result_entity_links(message: Any) -> list[SearchResultEntityLink]:
    links: list[SearchResultEntityLink] = []
    for entity in getattr(message, "entities", None) or []:
        if type(entity).__name__ != "MessageEntityTextUrl":
            continue
        url = str(getattr(entity, "url", "") or "").strip()
        username = _telegram_username(url)
        if not username:
            continue
        links.append(
            SearchResultEntityLink(
                position=len(links) + 1,
                url=url,
                username=username,
            )
        )
    return links


def find_target_entity_link(
    links: Iterable[SearchResultEntityLink],
    target_username: str,
) -> SearchResultEntityLink | None:
    expected = target_username.strip().lower().lstrip("@")
    return next(
        (
            link
            for link in links
            if link.username.lower() == expected
        ),
        None,
    )


def is_group_result_entity_page(
    links: list[SearchResultEntityLink],
    buttons: Iterable[Any],
) -> bool:
    first = next(iter(buttons), None)
    return bool(
        links
        and first is not None
        and str(getattr(first, "button_type", "") or "")
        == "callback_data"
        and _visible_text(getattr(first, "text", "")) == "🔄"
    )


def approved_navigation_positions(
    buttons: Iterable[Any],
) -> frozenset[int]:
    return frozenset(
        int(getattr(button, "position", 0) or 0)
        for button in buttons
        if getattr(button, "button_type", "") == "callback_data"
        and getattr(button, "effect", "") == "navigate_only"
    )


async def open_target_entity(
    client: Any,
    link: SearchResultEntityLink,
    target_username: str,
) -> TargetOpenEvidence:
    expected = target_username.strip().lower().lstrip("@")
    if link.username.lower() != expected:
        raise RuntimeError("target_entity_link_identity_mismatch")
    entity = await client.get_entity(link.url)
    full = await client(
        functions.channels.GetFullChannelRequest(channel=entity)
    )
    opened = _matching_opened_entity(
        getattr(full, "chats", None) or [],
        entity,
        expected,
    )
    username = str(getattr(opened, "username", "") or "")
    if username.lower().lstrip("@") != expected:
        raise RuntimeError("target_entity_remote_identity_mismatch")
    entity_id = str(getattr(opened, "id", "") or "")
    if not entity_id:
        raise RuntimeError("target_entity_remote_id_missing")
    return TargetOpenEvidence(
        entity_id=entity_id,
        title=str(getattr(opened, "title", "") or ""),
        username=username,
    )


def target_entity_fingerprint(
    link: SearchResultEntityLink,
    evidence: TargetOpenEvidence,
) -> str:
    payload = {
        "position": link.position,
        "url": link.url.lower(),
        "entity_id": evidence.entity_id,
        "username": evidence.username.lower(),
        "rpc": evidence.rpc,
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _telegram_username(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return ""
    if parsed.netloc.lower() not in TELEGRAM_HOSTS:
        return ""
    path_parts = [part for part in parsed.path.split("/") if part]
    if len(path_parts) != 1 or parsed.query or parsed.fragment:
        return ""
    username = path_parts[0].lstrip("@")
    return username if USERNAME_PATTERN.fullmatch(username) else ""


def _matching_opened_entity(
    chats: Iterable[Any],
    fallback: Any,
    expected: str,
) -> Any:
    return next(
        (
            chat
            for chat in chats
            if str(getattr(chat, "username", "") or "")
            .lower()
            .lstrip("@")
            == expected
        ),
        fallback,
    )


def _visible_text(value: object) -> str:
    return str(value or "").strip().replace("\ufe0f", "")


__all__ = [
    "SearchResultEntityLink",
    "TargetOpenEvidence",
    "approved_navigation_positions",
    "find_target_entity_link",
    "is_group_result_entity_page",
    "open_target_entity",
    "search_result_entity_links",
    "target_entity_fingerprint",
]

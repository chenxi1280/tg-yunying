from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse


TELEGRAM_HOSTS = {"t.me", "telegram.me", "www.t.me", "www.telegram.me"}
NAVIGATION_MARKERS = ("下一页", "上一页", "next", "prev", "page", "页")
GROUP_CATEGORY_TEXTS = {"👥", "群组", "群聊", "groups", "group"}


@dataclass(frozen=True)
class SearchJoinButton:
    row: int
    col: int
    text: str
    button_type: str
    effect: str
    position: int
    url: str = ""
    target_username: str = ""
    target_chat_id: int | None = None


async def execute_search_join_with_client(client: Any, payload: dict[str, Any], *, keyword_text: str) -> dict[str, Any]:
    bot_username = _bot_username(payload)
    if not keyword_text.strip():
        return _failed("keyword_text_missing", "搜索关键词缺失")
    target = _target_spec(payload)
    try:
        return await _execute_search_pages(client, bot_username, keyword_text.strip(), payload, target)
    except Exception as exc:  # Telethon RPC errors are mapped at this adapter boundary.
        return _failed("search_join_execution_failed", str(exc) or exc.__class__.__name__)


async def _execute_search_pages(client: Any, bot_username: str, keyword_text: str, payload: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    max_pages = max(1, int(payload.get("max_pages") or 3))
    decoys: list[dict[str, Any]] = []
    total_results = 0
    bot = bot_username.strip().lstrip("@")
    async with client.conversation(bot, timeout=60) as conv:
        await conv.send_message("/start")
        await conv.get_response()
        await conv.send_message(keyword_text)
        page = await conv.get_response()
        page = await _open_group_results_page(conv, page)
        for page_no in range(1, max_pages + 1):
            buttons = _parse_buttons(page)
            total_results += len(buttons)
            await _click_page_decoys(page, buttons, payload, decoys)
            target_button = _find_target_button(buttons, target)
            if target_button:
                return await _execute_target_join(client, page, payload, target, target_button, decoys, page_no, total_results)
            next_button = _find_next_button(buttons)
            if next_button is None or page_no == max_pages:
                break
            await _click_button(page, next_button)
            page = await conv.get_response()
    return {**_failed("target_not_in_results", "目标群未出现在搜索结果"), "total_results": total_results}


async def _open_group_results_page(conv: Any, page: Any) -> Any:
    buttons = _parse_buttons(page)
    group_button = _find_group_category_button(buttons)
    if group_button is None:
        return page
    await _click_button(page, group_button)
    return await conv.get_response()


def _find_group_category_button(buttons: list[SearchJoinButton]) -> SearchJoinButton | None:
    for button in buttons:
        if button.effect != "unknown":
            continue
        if button.text.strip().lower() in GROUP_CATEGORY_TEXTS:
            return button
    return None


async def _click_page_decoys(page: Any, buttons: list[SearchJoinButton], payload: dict[str, Any], decoys: list[dict[str, Any]]) -> None:
    limit = int(_safe(payload).get("pre_join_decoy_click_max") or 0)
    if len(decoys) >= limit:
        return
    clicked = await _click_safe_navigation(page, buttons, limit - len(decoys))
    decoys.extend(clicked)


async def _execute_target_join(
    client: Any,
    page: Any,
    payload: dict[str, Any],
    target: dict[str, Any],
    button: SearchJoinButton,
    decoys: list[dict[str, Any]],
    page_no: int,
    total: int,
) -> dict[str, Any]:
    if button.button_type == "external_http_url":
        return _external_blocked(button, total, decoys)
    click_result = await _click_button(page, button)
    joined_target = await _join_target(client, button, target, click_result)
    await _mark_read_if_supported(client, joined_target)
    return _success(payload, button, total, decoys, page_no)


def _parse_buttons(message: Any) -> list[SearchJoinButton]:
    result: list[SearchJoinButton] = []
    position = 1
    for row_index, row in enumerate(getattr(message, "buttons", None) or []):
        for col_index, raw in enumerate(row):
            text = _button_text(raw)
            url = _button_url(raw)
            result.append(
                SearchJoinButton(
                    row=row_index,
                    col=col_index,
                    text=text,
                    button_type=_button_type(raw, url),
                    effect=_button_effect(raw, text, url),
                    position=position,
                    url=url,
                    target_username=_telegram_username(url),
                    target_chat_id=_target_chat_id(raw),
                )
            )
            position += 1
    return result


def _button_text(button: Any) -> str:
    for candidate in (button, getattr(button, "button", None)):
        text = str(getattr(candidate, "text", "") or "").strip()
        if text:
            return text
    return ""


def _button_url(button: Any) -> str:
    for candidate in (button, getattr(button, "button", None)):
        url = str(getattr(candidate, "url", "") or "").strip()
        if url:
            return url
    return ""


def _button_type(button: Any, url: str) -> str:
    if url:
        host = (urlparse(url).netloc or "").lower()
        return "telegram_url" if host in TELEGRAM_HOSTS else "external_http_url"
    data = getattr(button, "data", None) or getattr(getattr(button, "button", None), "data", None)
    return "callback_data" if data else "unknown"


def _button_effect(button: Any, text: str, url: str) -> str:
    explicit = str(getattr(button, "effect", "") or getattr(button, "button_effect", "") or "").strip()
    if explicit:
        return explicit
    if _is_navigation_text(text):
        return "navigate_only"
    if url and (urlparse(url).netloc or "").lower() not in TELEGRAM_HOSTS:
        return "external"
    if url:
        return "join_candidate"
    return "unknown"


async def _click_safe_navigation(message: Any, buttons: list[SearchJoinButton], limit: int) -> list[dict[str, Any]]:
    clicked: list[dict[str, Any]] = []
    for button in buttons:
        if len(clicked) >= limit:
            break
        if button.effect != "navigate_only" or _is_page_nav_button(button):
            continue
        await _click_button(message, button)
        clicked.append({"position": button.position, "button_hash": _button_hash(button), "effect": button.effect, "joined": False})
    return clicked


def _find_target_button(buttons: list[SearchJoinButton], target: dict[str, Any]) -> SearchJoinButton | None:
    for button in buttons:
        if _matches_target(button, target):
            return button
    return None


def _find_next_button(buttons: list[SearchJoinButton]) -> SearchJoinButton | None:
    for button in buttons:
        text = button.text.lower()
        if "下一页" in text or "next" in text:
            return button
    return None


def _matches_target(button: SearchJoinButton, target: dict[str, Any]) -> bool:
    username = str(target.get("username") or "").strip().lower().lstrip("@")
    title = str(target.get("title") or "").strip().lower()
    target_id = int(target.get("group_id") or 0)
    if username and button.target_username.lower() == username:
        return True
    if target_id and button.target_chat_id == target_id:
        return True
    return bool(title and title in button.text.lower())


async def _click_button(message: Any, button: SearchJoinButton) -> Any:
    return await message.click(button.row, button.col)


async def _join_target(client: Any, button: SearchJoinButton, target: dict[str, Any], click_result: Any = None) -> str:
    url_ref = _click_result_url(click_result) or button.url
    invite_hash = _invite_hash(url_ref)
    if invite_hash:
        await _import_invite(client, invite_hash)
        return invite_hash
    join_ref = _telegram_username(url_ref) or button.target_username or str(target.get("username") or target.get("group_id") or "")
    if button.button_type == "callback_data" and not join_ref:
        raise RuntimeError("callback button did not expose a join target")
    if not join_ref:
        raise RuntimeError("target join reference missing")
    entity = await client.get_entity(join_ref)
    await _join_channel(client, entity)
    return str(entity)


async def _join_channel(client: Any, entity: Any) -> None:
    from telethon import functions

    await client(functions.channels.JoinChannelRequest(channel=entity))


async def _import_invite(client: Any, invite_hash: str) -> None:
    from telethon import functions

    await client(functions.messages.ImportChatInviteRequest(invite_hash))


async def _mark_read_if_supported(client: Any, target: str) -> None:
    mark_read = getattr(client, "mark_read", None)
    if callable(mark_read):
        await mark_read(target)


def _external_blocked(button: SearchJoinButton, total: int, decoys: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        **_failed("external_url_requires_web_profile", "外部 HTTP URL 需要 Web Profile，首版不执行"),
        "target_position": button.position,
        "total_results": total,
        "pre_join_decoy_clicks": decoys,
    }


def _success(payload: dict[str, Any], button: SearchJoinButton, total: int, decoys: list[dict[str, Any]], page_no: int) -> dict[str, Any]:
    return {
        "success": True,
        "join_status": "membership_observed",
        "target_position": button.position,
        "page": page_no,
        "total_results": total,
        "target_group_id": payload.get("target_group_id"),
        "pre_join_decoy_clicks": decoys,
        "post_join_safe_navigation": [],
        "post_join_policy": payload.get("post_join_policy") or "stay_joined",
        "keyword_hash": payload.get("keyword_hash"),
    }


def _failed(code: str, detail: str) -> dict[str, Any]:
    return {"success": False, "error_code": code, "detail": detail, "join_status": "failed"}


def _bot_username(payload: dict[str, Any]) -> str:
    return str(payload.get("bot_username") or "").strip().lstrip("@")


def _safe(payload: dict[str, Any]) -> dict[str, Any]:
    safe = payload.get("safe_navigation")
    return safe if isinstance(safe, dict) else {}


def _target_spec(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "username": payload.get("target_username"),
        "group_id": _telegram_channel_id(payload.get("target_peer_id")) or payload.get("target_group_id"),
        "title": payload.get("target_title"),
    }


def _telegram_channel_id(value: Any) -> int:
    text = str(value or "").strip()
    if not text.lstrip("-").isdigit():
        return 0
    if text.startswith("-100") and len(text) > 4:
        return int(text[4:])
    return abs(int(text))


def _telegram_username(url: str) -> str:
    parsed = urlparse(url)
    if (parsed.netloc or "").lower() not in TELEGRAM_HOSTS:
        return ""
    path = parsed.path.strip("/")
    if not path or path.startswith("+") or path.startswith("joinchat/"):
        return ""
    return path.split("/", 1)[0].lstrip("@")


def _invite_hash(url: str) -> str:
    parsed = urlparse(url)
    if (parsed.netloc or "").lower() not in TELEGRAM_HOSTS:
        return ""
    path = parsed.path.strip("/")
    if path.startswith("+"):
        return path[1:]
    if path.startswith("joinchat/"):
        return path.split("/", 1)[1]
    return ""


def _click_result_url(result: Any) -> str:
    return str(getattr(result, "url", "") or "").strip()


def _target_chat_id(button: Any) -> int | None:
    raw = getattr(button, "target_chat_id", None) or getattr(getattr(button, "button", None), "target_chat_id", None)
    return int(raw) if str(raw or "").lstrip("-").isdigit() else None


def _is_navigation_text(text: str) -> bool:
    normalized = text.strip().lower()
    return any(marker in normalized for marker in NAVIGATION_MARKERS)


def _is_page_nav_button(button: SearchJoinButton) -> bool:
    text = button.text.lower()
    return "下一页" in text or "上一页" in text or "next" in text or "prev" in text


def _button_hash(button: SearchJoinButton) -> str:
    return str(abs(hash((button.text, button.url, button.position))))[:16]


__all__ = ["SearchJoinButton", "execute_search_join_with_client"]

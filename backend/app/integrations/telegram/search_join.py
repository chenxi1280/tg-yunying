from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse


TELEGRAM_HOSTS = {"t.me", "telegram.me", "www.t.me", "www.telegram.me"}
NAVIGATION_MARKERS = ("下一页", "上一页", "next", "prev", "page", "页")
HUMAN_VERIFICATION_MARKERS = ("人机验证", "计算结果", "captcha")
JISOU_BOT_USERNAMES = frozenset({"jisou"})
JISOU_GROUP_CATEGORY_TEXTS = frozenset({"👥", "群组", "群聊", "groups", "group", "👥群组", "👥群聊"})
PAGINATION_SYMBOL_NAMES = {
    ">": "greater_than",
    "▶": "right_triangle",
    "▷": "white_right_triangle",
    "➡": "right_arrow",
    "→": "right_arrow",
    "»": "right_double_angle",
    "›": "right_angle",
    "⏩": "fast_forward",
    "⏭": "next_track",
    "<": "less_than",
    "◀": "left_triangle",
    "◁": "white_left_triangle",
    "⬅": "left_arrow",
    "←": "left_arrow",
    "«": "left_double_angle",
    "‹": "left_angle",
    "⏪": "fast_reverse",
    "⏮": "previous_track",
}
NEXT_PAGE_SYMBOLS = frozenset({">", "▶", "▷", "➡", "→", "»", "›", "⏩", "⏭"})
VARIATION_SELECTOR = "\ufe0f"


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


@dataclass(frozen=True)
class TextTargetMatch:
    position: int
    line: str


async def execute_search_join_with_client(client: Any, payload: dict[str, Any], *, keyword_text: str) -> dict[str, Any]:
    bot_username = _bot_username(payload)
    if not keyword_text.strip():
        return _failed("keyword_text_missing", "搜索关键词缺失")
    target = _target_spec(payload)
    if not str(target.get("username") or "").strip():
        return _failed("target_identity_missing", "搜索入群目标缺少可验证 username")
    try:
        return await _execute_search_pages(client, bot_username, keyword_text.strip(), payload, target)
    except Exception as exc:  # Telethon RPC errors are mapped at this adapter boundary.
        return _failed("search_join_execution_failed", str(exc) or exc.__class__.__name__)


async def _execute_search_pages(client: Any, bot_username: str, keyword_text: str, payload: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    decoys: list[dict[str, Any]] = []
    total_results = 0
    page_no = 0
    bot = bot_username.strip().lstrip("@")
    async with client.conversation(bot, timeout=60) as conv:
        await conv.send_message("/start")
        await conv.get_response()
        await conv.send_message(keyword_text)
        page = await conv.get_response()
        page, selector_error, group_selector, selector_buttons = await _select_jisou_group_results_page(client, page, bot)
        if selector_error is not None:
            return selector_error
        while True:
            page_no += 1
            if _human_verification_required(page):
                return _failed("bot_human_verification_required", "搜索机器人要求人机验证，当前账号不能自动执行")
            buttons = _parse_buttons(page)
            total_results += len(buttons)
            await _click_page_decoys(page, buttons, payload, target, decoys)
            text_match = _find_target_in_text(page, target)
            if text_match:
                return await _execute_text_target_join(client, payload, target, text_match, decoys, page_no, total_results)
            target_button = _find_target_button(buttons, target)
            if target_button:
                return await _execute_target_join(client, page, payload, target, target_button, decoys, page_no, total_results)
            next_button = _find_next_button(buttons)
            if next_button is None:
                return _target_not_found(total_results, decoys, page_no, buttons, group_selector, selector_buttons)
            page = await _click_and_get_edited_page(client, bot, page, next_button)


async def _select_jisou_group_results_page(
    client: Any,
    page: Any,
    bot_username: str,
) -> tuple[Any, dict[str, Any] | None, SearchJoinButton | None, list[SearchJoinButton]]:
    if not _is_jisou_bot(bot_username):
        return page, None, None, []
    selector_buttons = _parse_buttons(page)
    group_button = _find_jisou_group_category_button(selector_buttons)
    if group_button is None:
        return page, _failed("jisou_group_selector_missing", "极搜群聊类型选择按钮缺失"), None, selector_buttons
    return await _click_and_get_edited_page(client, bot_username, page, group_button), None, group_button, selector_buttons


def _is_jisou_bot(bot_username: str) -> bool:
    return bot_username.strip().lower().lstrip("@") in JISOU_BOT_USERNAMES


def _find_jisou_group_category_button(buttons: list[SearchJoinButton]) -> SearchJoinButton | None:
    for button in buttons:
        if button.button_type != "callback_data" or button.effect != "unknown":
            continue
        if _normalized_button_text(button.text) in JISOU_GROUP_CATEGORY_TEXTS:
            return button
    return None


def _normalized_button_text(text: str) -> str:
    return re.sub(r"\s+", "", text).lower()


def _target_not_found(
    total: int,
    decoys: list[dict[str, Any]],
    page_no: int,
    buttons: list[SearchJoinButton],
    group_selector: SearchJoinButton | None,
    selector_buttons: list[SearchJoinButton],
) -> dict[str, Any]:
    return {
        **_failed("target_not_in_results", "目标群未出现在搜索结果"),
        "total_results": total,
        "pre_join_decoy_clicks": decoys,
        "page": page_no,
        "searched_pages": page_no,
        "last_result_page": page_no,
        "search_end_reason": "no_next_page",
        **_search_protocol_trace(buttons, group_selector, selector_buttons),
    }


def _search_protocol_trace(
    buttons: list[SearchJoinButton],
    group_selector: SearchJoinButton | None,
    selector_buttons: list[SearchJoinButton],
) -> dict[str, Any]:
    if group_selector is None:
        return {}
    return {
        "search_protocol_trace": {
            "jisou_group_selector": {"position": group_selector.position, "text": group_selector.text},
            "selector_page": _page_layout(selector_buttons),
            "result_page": _page_layout(buttons),
        }
    }


def _page_layout(buttons: list[SearchJoinButton]) -> dict[str, Any]:
    return {"button_count": len(buttons), "button_layout": [_button_layout(button) for button in buttons]}


def _button_layout(button: SearchJoinButton) -> dict[str, Any]:
    normalized = _normalized_button_text(button.text)
    return {
        "row": button.row,
        "col": button.col,
        "button_type": button.button_type,
        "effect": button.effect,
        "text_length": len(button.text),
        "contains_page_marker": any(marker in normalized for marker in NAVIGATION_MARKERS),
        "navigation_symbols": [name for symbol, name in PAGINATION_SYMBOL_NAMES.items() if symbol in button.text],
    }


async def _click_page_decoys(
    page: Any,
    buttons: list[SearchJoinButton],
    payload: dict[str, Any],
    target: dict[str, Any],
    decoys: list[dict[str, Any]],
) -> None:
    limit = int(_safe(payload).get("pre_join_decoy_click_max") or 0)
    if len(decoys) >= limit:
        return
    clicked = await _click_safe_navigation(page, buttons, target, limit - len(decoys))
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


async def _execute_text_target_join(
    client: Any,
    payload: dict[str, Any],
    target: dict[str, Any],
    match: TextTargetMatch,
    decoys: list[dict[str, Any]],
    page_no: int,
    total: int,
) -> dict[str, Any]:
    join_ref = str(target.get("username") or target.get("group_id") or "").strip()
    if not join_ref:
        return _failed("target_join_reference_missing", "正文命中目标但缺少可加入的 username / peer")
    entity = await client.get_entity(join_ref)
    await _join_channel(client, entity)
    await _mark_read_if_supported(client, str(entity))
    return {
        **_success(payload, None, total, decoys, page_no),
        "target_position": match.position,
        "target_match_source": "message_text",
        "target_line": match.line,
    }


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


async def _click_safe_navigation(
    message: Any,
    buttons: list[SearchJoinButton],
    target: dict[str, Any],
    limit: int,
) -> list[dict[str, Any]]:
    clicked: list[dict[str, Any]] = []
    for button in buttons:
        if len(clicked) >= limit:
            break
        if button.effect != "navigate_only" or _is_page_nav_button(button) or _matches_target(button, target):
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
        if _is_next_page_button(button):
            return button
    return None


def _is_next_page_button(button: SearchJoinButton) -> bool:
    if button.button_type != "callback_data":
        return False
    text = _normalized_button_text(button.text)
    if "下一页" in text or "next" in text:
        return True
    symbols = text.replace(VARIATION_SELECTOR, "")
    return bool(symbols) and all(symbol in NEXT_PAGE_SYMBOLS for symbol in symbols)


def _matches_target(button: SearchJoinButton, target: dict[str, Any]) -> bool:
    username = str(target.get("username") or "").strip().lower().lstrip("@")
    if username and button.target_username.lower() == username:
        return True
    return False


async def _click_button(message: Any, button: SearchJoinButton) -> Any:
    return await message.click(button.row, button.col)


async def _click_and_get_edited_page(client: Any, bot_username: str, message: Any, button: SearchJoinButton) -> Any:
    await _click_button(message, button)
    edited_page = await client.get_messages(bot_username, ids=message.id)
    if edited_page is None:
        raise RuntimeError("callback edited message unavailable")
    return edited_page


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

    try:
        await client(functions.channels.JoinChannelRequest(channel=entity))
    except Exception as exc:
        if _is_already_participant_error(exc):
            return
        raise


async def _import_invite(client: Any, invite_hash: str) -> None:
    from telethon import functions

    try:
        await client(functions.messages.ImportChatInviteRequest(invite_hash))
    except Exception as exc:
        if _is_already_participant_error(exc):
            return
        raise


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


def _success(payload: dict[str, Any], button: SearchJoinButton | None, total: int, decoys: list[dict[str, Any]], page_no: int) -> dict[str, Any]:
    return {
        "success": True,
        "join_status": "membership_observed",
        "target_position": button.position if button else 0,
        "page": page_no,
        "searched_pages": page_no,
        "last_result_page": page_no,
        "search_end_reason": "target_found",
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


def _find_target_in_text(message: Any, target: dict[str, Any]) -> TextTargetMatch | None:
    username = str(target.get("username") or "").strip().lower().lstrip("@")
    if not username:
        return None
    pattern = re.compile(rf"(?<![a-z0-9_])@?{re.escape(username)}(?![a-z0-9_])")
    for position, line in enumerate(_message_text(message).splitlines(), start=1):
        normalized = line.strip().lower()
        if not normalized:
            continue
        if pattern.search(normalized):
            return TextTargetMatch(position, line.strip())
    return None


def _human_verification_required(message: Any) -> bool:
    text = _message_text(message).lower()
    return any(marker in text for marker in HUMAN_VERIFICATION_MARKERS)


def _message_text(message: Any) -> str:
    return str(getattr(message, "raw_text", "") or getattr(message, "message", "") or "")


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


def _is_already_participant_error(exc: Exception) -> bool:
    text = f"{exc.__class__.__name__} {exc}".lower()
    return "already" in text and ("participant" in text or "member" in text)


def _is_navigation_text(text: str) -> bool:
    normalized = text.strip().lower()
    return any(marker in normalized for marker in NAVIGATION_MARKERS)


def _is_page_nav_button(button: SearchJoinButton) -> bool:
    text = button.text.lower()
    return "下一页" in text or "上一页" in text or "next" in text or "prev" in text


def _button_hash(button: SearchJoinButton) -> str:
    raw = f"{button.text}:{button.url}:{button.position}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


__all__ = ["SearchJoinButton", "execute_search_join_with_client"]

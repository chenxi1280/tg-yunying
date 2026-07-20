from __future__ import annotations

import asyncio
import random
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse


MAX_RANK_DEBOOST_PAGES = 10
TELEGRAM_HOSTS = {"t.me", "telegram.me", "www.t.me", "www.telegram.me"}
PUBLIC_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,}$")


@dataclass(frozen=True)
class RankDeboostButton:
    row: int
    col: int
    text: str
    effect: str
    url: str = ""
    target_username: str = ""


@dataclass(frozen=True)
class RankDeboostClickSelection:
    result: dict[str, Any]
    button: RankDeboostButton


async def search_rank_deboost_candidates_with_client(client: Any, payload: dict[str, Any], *, keyword_text: str) -> dict[str, Any]:
    keyword = keyword_text.strip()
    if not keyword:
        return _failed("keyword_text_missing", "搜索关键词缺失", [])
    results: list[dict[str, Any]] = []
    bot = _bot_username(payload)
    async with client.conversation(bot, timeout=60) as conv:
        page = await _start_search(conv, keyword)
        for page_no in range(1, MAX_RANK_DEBOOST_PAGES + 1):
            results.extend(_parse_search_results(page, page_no=page_no))
            next_button = _next_page_button(page)
            if next_button is None or page_no == MAX_RANK_DEBOOST_PAGES:
                return _candidates_found(results, page_no)
            await page.click(next_button.row, next_button.col)
            page = await conv.get_response()
    return _candidates_found(results, MAX_RANK_DEBOOST_PAGES)


async def execute_rank_deboost_with_client(client: Any, payload: dict[str, Any], *, keyword_text: str) -> dict[str, Any]:
    keyword = keyword_text.strip()
    if not keyword:
        return _failed("keyword_text_missing", "搜索关键词缺失", [])
    if not _target_identities(payload)[0]:
        return _no_click("target_identity_missing", [], pages_scanned=0)
    try:
        dwell_seconds = _dwell_seconds(payload)
    except ValueError as exc:
        return _no_click("invalid_dwell_range", [], detail=str(exc), pages_scanned=0)
    results: list[dict[str, Any]] = []
    bot = _bot_username(payload)
    async with client.conversation(bot, timeout=60) as conv:
        page = await _start_search(conv, keyword)
        for page_no in range(1, MAX_RANK_DEBOOST_PAGES + 1):
            page_results = _parse_search_results(page, page_no=page_no)
            results.extend(page_results)
            selection, status = _click_selection(page, payload, page_results)
            if selection is not None:
                return await _click_selected_result(page, results, selection, dwell_seconds, page_no)
            if status != "target_not_in_results":
                return _no_click(status, results, pages_scanned=page_no)
            next_button = _next_page_button(page)
            if next_button is None or page_no == MAX_RANK_DEBOOST_PAGES:
                return _no_click(status, results, pages_scanned=page_no)
            await page.click(next_button.row, next_button.col)
            page = await conv.get_response()
    return _no_click("target_not_in_results", results, pages_scanned=MAX_RANK_DEBOOST_PAGES)


async def _start_search(conv: Any, keyword: str) -> Any:
    await conv.send_message("/start")
    await conv.get_response()
    await conv.send_message(keyword)
    return await conv.get_response()


def _candidates_found(results: list[dict[str, Any]], pages_scanned: int) -> dict[str, Any]:
    return {
        "success": True,
        "execution_status": "candidates_found",
        "search_results": results,
        "pages_scanned": pages_scanned,
    }


def _click_selection(
    message: Any,
    payload: dict[str, Any],
    results: list[dict[str, Any]],
) -> tuple[RankDeboostClickSelection | None, str]:
    target_position, target_status = _target_position(results, payload)
    if target_position is None:
        return None, target_status
    candidates = _eligible_results(results, payload, target_position)
    if not candidates:
        return None, "all_exempt_clicks"
    for result in candidates:
        button = _button_for_result(message, str(result.get("username") or ""))
        if button is not None:
            return RankDeboostClickSelection(result=result, button=button), ""
    return None, "no_navigable_button"


def _target_position(results: list[dict[str, Any]], payload: dict[str, Any]) -> tuple[int | None, str]:
    target_usernames, target_peer_ids = _target_identities(payload)
    if not target_usernames:
        return None, "target_identity_missing"
    positions = [
        int(result["position"])
        for result in results
        if _is_target_result(result, target_usernames, target_peer_ids)
    ]
    return (min(positions), "") if positions else (None, "target_not_in_results")


def _eligible_results(results: list[dict[str, Any]], payload: dict[str, Any], target_position: int) -> list[dict[str, Any]]:
    exempt_username = str(payload.get("exempt_group_username") or "").strip().lower().lstrip("@")
    target_usernames, target_peer_ids = _target_identities(payload)
    return [
        result for result in results
        if int(result["position"]) < target_position
        and _result_username(result) != exempt_username
        and not _is_target_result(result, target_usernames, target_peer_ids)
    ]


def _target_identities(payload: dict[str, Any]) -> tuple[set[str], set[str]]:
    refs = payload.get("target_group_refs")
    if not isinstance(refs, list):
        return set(), set()
    usernames = {
        _result_username(item)
        for item in refs
        if isinstance(item, dict) and _result_username(item)
    }
    peer_ids = {
        str(item.get("peer_id") or "").strip()
        for item in refs
        if isinstance(item, dict) and str(item.get("peer_id") or "").strip()
    }
    return usernames, peer_ids


def _is_target_result(result: dict[str, Any], target_usernames: set[str], target_peer_ids: set[str]) -> bool:
    username = _result_username(result)
    peer_id = str(result.get("peer_id") or "").strip()
    return (bool(username) and username in target_usernames) or (bool(peer_id) and peer_id in target_peer_ids)


def _result_username(result: dict[str, Any]) -> str:
    return str(result.get("username") or "").strip().lower().lstrip("@")


def _button_for_result(message: Any, username: str) -> RankDeboostButton | None:
    normalized_username = username.strip().lower().lstrip("@")
    for row_index, row in enumerate(getattr(message, "buttons", None) or []):
        for col_index, raw in enumerate(row):
            button = _normalize_button(raw, row_index, col_index)
            if not button.url and button.effect == "navigate_only" and button.target_username == normalized_username:
                return button
    return None


def _next_page_button(message: Any) -> RankDeboostButton | None:
    for row_index, row in enumerate(getattr(message, "buttons", None) or []):
        for col_index, raw in enumerate(row):
            button = _normalize_button(raw, row_index, col_index)
            if not button.url and button.effect == "navigate_only" and _is_next_page_text(button.text):
                return button
    return None


async def _click_selected_result(
    page: Any,
    results: list[dict[str, Any]],
    selection: RankDeboostClickSelection,
    dwell_seconds: int,
    pages_scanned: int,
) -> dict[str, Any]:
    started_at = time.monotonic()
    try:
        await page.click(selection.button.row, selection.button.col)
        await asyncio.sleep(dwell_seconds)
    except Exception as exc:  # noqa: BLE001 - Telegram may receive the click before local transport fails.
        return _unknown_after_click(selection, results, started_at, exc, pages_scanned)
    outcome = _click_outcome(selection, _elapsed_seconds(started_at))
    return {
        "success": True,
        "execution_status": "confirmed",
        "search_results": results,
        "pages_scanned": pages_scanned,
        "click_outcomes": [{**outcome, "status": "confirmed"}],
    }


def _unknown_after_click(
    selection: RankDeboostClickSelection,
    results: list[dict[str, Any]],
    started_at: float,
    exc: Exception,
    pages_scanned: int,
) -> dict[str, Any]:
    outcome = _click_outcome(selection, _elapsed_seconds(started_at))
    return {
        **_failed("unknown_after_click", str(exc) or exc.__class__.__name__, results),
        "execution_status": "unknown_after_click",
        "pages_scanned": pages_scanned,
        "click_outcomes": [{**outcome, "status": "unknown_after_click"}],
    }


def _click_outcome(selection: RankDeboostClickSelection, dwell_seconds: int) -> dict[str, Any]:
    result = selection.result
    button = selection.button
    return {
        "competitor_username": str(result.get("username") or ""),
        "competitor_peer_id": str(result.get("peer_id") or ""),
        "competitor_title": str(result.get("title") or ""),
        "competitor_position": int(result["position"]),
        "row": button.row,
        "col": button.col,
        "text": button.text,
        "url": button.url,
        "effect": button.effect,
        "joined": False,
        "dwell_seconds": dwell_seconds,
    }


def _dwell_seconds(payload: dict[str, Any]) -> int:
    minimum = int(payload.get("dwell_seconds_min") or 0)
    maximum = int(payload.get("dwell_seconds_max") or minimum)
    if minimum < 0 or maximum < minimum:
        raise ValueError("dwell_seconds_max must be greater than or equal to dwell_seconds_min")
    return random.randint(minimum, maximum)


def _elapsed_seconds(started_at: float) -> int:
    return max(0, int(round(time.monotonic() - started_at)))


def _bot_username(payload: dict[str, Any]) -> str:
    bot = str(payload.get("bot_username") or "").strip().lstrip("@")
    return bot or "jisou"


def _parse_search_results(message: Any, *, page_no: int) -> list[dict[str, Any]]:
    text = str(getattr(message, "message", "") or "")
    results: list[dict[str, Any]] = []
    for match in re.finditer(r"(?:^|\n)\s*(\d+)[\.、\)]\s*@?([A-Za-z0-9_]{3,})", text):
        results.append({
            "position": int(match.group(1)),
            "username": match.group(2).lstrip("@"),
            "page": page_no,
        })
    return results


def _normalize_button(raw: Any, row: int, col: int) -> RankDeboostButton:
    text = _button_text(raw)
    url = str(getattr(raw, "url", "") or getattr(getattr(raw, "button", None), "url", "") or "").strip()
    return RankDeboostButton(
        row=row,
        col=col,
        text=text,
        effect=str(getattr(raw, "effect", "") or getattr(raw, "button_effect", "") or _infer_effect(text)).strip(),
        url=url,
        target_username=_telegram_username(url),
    )


def _button_text(raw: Any) -> str:
    for candidate in (raw, getattr(raw, "button", None)):
        text = str(getattr(candidate, "text", "") or "").strip()
        if text:
            return text
    return ""


def _telegram_username(url: str) -> str:
    parsed = urlparse(url)
    if (parsed.netloc or "").lower() not in TELEGRAM_HOSTS:
        return ""
    path = parsed.path.strip("/")
    if not path or "/" in path or path.startswith("+"):
        return ""
    username = path.lstrip("@")
    return username.lower() if PUBLIC_USERNAME_RE.fullmatch(username) else ""


def _infer_effect(text: str) -> str:
    normalized = text.lower()
    if _is_next_page_text(normalized) or any(marker in normalized for marker in ("详情", "detail", "view", "查看")):
        return "navigate_only"
    return "unknown"


def _is_next_page_text(text: str) -> bool:
    normalized = text.lower()
    return "下一页" in normalized or "next" in normalized


def _no_click(
    status: str,
    search_results: list[dict[str, Any]],
    *,
    detail: str = "未找到可验证身份的安全导航按钮",
    pages_scanned: int,
) -> dict[str, Any]:
    return {
        "success": False,
        "execution_status": status,
        "detail": detail,
        "search_results": search_results,
        "pages_scanned": pages_scanned,
        "click_outcomes": [],
    }


def _failed(code: str, detail: str, search_results: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "success": False,
        "error_code": code,
        "detail": detail,
        "search_results": search_results,
        "click_outcomes": [],
    }


__all__ = [
    "RankDeboostButton",
    "execute_rank_deboost_with_client",
    "search_rank_deboost_candidates_with_client",
]

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


MAX_RANK_DEBOOST_PAGES = 10


@dataclass(frozen=True)
class RankDeboostButton:
    row: int
    col: int
    text: str
    effect: str
    url: str = ""


async def search_rank_deboost_candidates_with_client(client: Any, payload: dict[str, Any], *, keyword_text: str) -> dict[str, Any]:
    page = await _first_search_page(client, payload, keyword_text)
    if isinstance(page, dict):
        return page
    return {
        "success": True,
        "execution_status": "candidates_found",
        "search_results": _parse_search_results(page, page_no=1),
        "pages_scanned": 1,
    }


async def execute_rank_deboost_with_client(client: Any, payload: dict[str, Any], *, keyword_text: str) -> dict[str, Any]:
    page = await _first_search_page(client, payload, keyword_text)
    if isinstance(page, dict):
        return page
    search_results = _parse_search_results(page, page_no=1)
    button = _first_safe_button(page)
    if button is None:
        return _failed("no_navigable_button", "搜索结果无安全可点击按钮", search_results)
    outcome = {"row": button.row, "col": button.col, "effect": button.effect}
    try:
        await page.click(button.row, button.col)
    except Exception as exc:  # noqa: BLE001 - click may have reached Telegram before the local exception.
        return {
            **_failed("unknown_after_click", str(exc) or exc.__class__.__name__, search_results),
            "execution_status": "unknown_after_click",
            "click_outcomes": [{**outcome, "status": "unknown_after_click"}],
        }
    return {
        "success": True,
        "execution_status": "confirmed",
        "search_results": search_results,
        "click_outcomes": [{**outcome, "status": "confirmed"}],
    }


async def _first_search_page(client: Any, payload: dict[str, Any], keyword_text: str) -> Any:
    keyword = keyword_text.strip()
    if not keyword:
        return _failed("keyword_text_missing", "搜索关键词缺失", [])
    bot = _bot_username(payload)
    async with client.conversation(bot, timeout=60) as conv:
        await conv.send_message("/start")
        await conv.get_response()
        await conv.send_message(keyword)
        return await conv.get_response()


def _bot_username(payload: dict[str, Any]) -> str:
    bot = str(payload.get("bot_username") or "").strip().lstrip("@")
    return bot or "jisou"


def _parse_search_results(message: Any, *, page_no: int) -> list[dict[str, Any]]:
    text = str(getattr(message, "message", "") or "")
    results: list[dict[str, Any]] = []
    for match in re.finditer(r"(?:^|\n)\s*(\d+)[\.\)、\)]\s*@?([A-Za-z0-9_]{3,})", text):
        results.append({
            "position": int(match.group(1)),
            "username": match.group(2).lstrip("@"),
            "page": page_no,
        })
    return results


def _first_safe_button(message: Any) -> RankDeboostButton | None:
    for row_index, row in enumerate(getattr(message, "buttons", None) or []):
        for col_index, raw in enumerate(row):
            button = _normalize_button(raw, row_index, col_index)
            if button.effect == "navigate_only":
                return button
    return None


def _normalize_button(raw: Any, row: int, col: int) -> RankDeboostButton:
    text = _button_text(raw)
    return RankDeboostButton(
        row=row,
        col=col,
        text=text,
        effect=str(getattr(raw, "effect", "") or getattr(raw, "button_effect", "") or _infer_effect(text)).strip(),
        url=str(getattr(raw, "url", "") or getattr(getattr(raw, "button", None), "url", "") or "").strip(),
    )


def _button_text(raw: Any) -> str:
    for candidate in (raw, getattr(raw, "button", None)):
        text = str(getattr(candidate, "text", "") or "").strip()
        if text:
            return text
    return ""


def _infer_effect(text: str) -> str:
    normalized = text.lower()
    if any(marker in normalized for marker in ("详情", "detail", "view", "查看")):
        return "navigate_only"
    return "unknown"


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

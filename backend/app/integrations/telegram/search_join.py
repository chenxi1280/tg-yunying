from __future__ import annotations

import asyncio
import hashlib
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlparse

from app.search_join_protocol import (
    VERIFICATION_IMAGE_PAGE,
    ProtocolPageClassification,
    classify_jisou_page_with_media,
    is_jisou_bot,
    normalize_visible_text,
)


TELEGRAM_HOSTS = {"t.me", "telegram.me", "www.t.me", "www.telegram.me"}
NAVIGATION_MARKERS = ("下一页", "上一页", "next", "prev", "page", "页")
HUMAN_VERIFICATION_MARKERS = ("人机验证", "计算结果", "captcha")

# PRD §2.19.2 图片算式验证码识别相关常量。
IMAGE_VERIFICATION_MIN_CONFIDENCE = 0.70
IMAGE_VERIFICATION_PROMPT = (
    "识别图片中的算式并计算结果，输出 JSON：{\"answer\":数字,\"confidence\":0到1}。"
    "answer 必须是算式计算后的正整数。只输出紧凑 JSON，不要解释。"
)
ImageVerificationSolver = Callable[[bytes, str, list[str]], "tuple[str, float] | None"]
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
    source: str


class _JoinRequestPendingError(Exception):
    pass


class _MembershipNotObservedError(Exception):
    pass


class ImageVerificationProviderUnavailableError(RuntimeError):
    pass


class ImageVerificationNoSafeAnswerError(RuntimeError):
    pass


async def execute_search_join_with_client(
    client: Any,
    payload: dict[str, Any],
    *,
    keyword_text: str,
    image_verification_solver: ImageVerificationSolver | None = None,
) -> dict[str, Any]:
    bot_username = _bot_username(payload)
    if not keyword_text.strip():
        return _failed("keyword_text_missing", "搜索关键词缺失")
    target = _target_spec(payload)
    if not str(target.get("username") or "").strip():
        return _failed("target_identity_missing", "搜索入群目标缺少可验证 username")
    try:
        return await _execute_search_pages(
            client,
            bot_username,
            keyword_text.strip(),
            payload,
            target,
            image_verification_solver=image_verification_solver,
        )
    except Exception as exc:  # Telethon RPC errors are mapped at this adapter boundary.
        return _failed("search_join_execution_failed", str(exc) or exc.__class__.__name__)


async def ensure_search_join_membership_with_client(client: Any, payload: dict[str, Any]) -> dict[str, Any]:
    target = _target_spec(payload)
    join_ref = _target_join_ref(target)
    if not join_ref:
        return _failed("target_join_reference_missing", "搜索命中目标缺少可加入的 username / peer")
    try:
        entity = await client.get_entity(join_ref)
        await _join_channel(client, entity)
    except _JoinRequestPendingError:
        return _join_request_pending(_membership_observed_result(payload))
    except Exception as exc:
        return _failed("search_join_membership_failed", str(exc) or exc.__class__.__name__)
    return _membership_observed_result(payload)


async def probe_search_join_membership_with_client(client: Any, payload: dict[str, Any]) -> dict[str, Any]:
    target = _target_spec(payload)
    join_ref = _target_join_ref(target)
    if not join_ref:
        return _failed("target_join_reference_missing", "搜索命中目标缺少可复核的 username / peer")
    try:
        entity = await client.get_entity(join_ref)
        await _assert_current_account_is_member(client, entity)
    except _MembershipNotObservedError:
        return _membership_not_observed(payload)
    except Exception as exc:
        return _failed("search_join_membership_probe_failed", str(exc) or exc.__class__.__name__)
    return _membership_observed_result(payload)


async def _execute_search_pages(
    client: Any,
    bot_username: str,
    keyword_text: str,
    payload: dict[str, Any],
    target: dict[str, Any],
    *,
    image_verification_solver: ImageVerificationSolver | None = None,
) -> dict[str, Any]:
    bot = bot_username.strip().lstrip("@")
    protocol_profile = payload.get("approved_protocol_profile")
    jisou = is_jisou_bot(bot)
    bootstrap_required = not await _has_jisou_conversation_history(client, bot) if jisou else True
    async with client.conversation(bot, timeout=60) as conv:
        page, recovery = await _initial_search_page(
            conv,
            keyword_text,
            jisou=jisou,
            bootstrap_required=bootstrap_required,
            protocol_profile=protocol_profile,
        )
        result = await _execute_search_result_pages(
            client,
            page,
            bot,
            payload,
            target,
            protocol_profile=protocol_profile,
            image_verification_solver=image_verification_solver,
        )
    return {**result, **recovery}


async def _initial_search_page(
    conversation: Any,
    keyword_text: str,
    *,
    jisou: bool,
    bootstrap_required: bool,
    protocol_profile: object,
) -> tuple[Any, dict[str, Any]]:
    if bootstrap_required:
        await conversation.send_message("/start")
        await conversation.get_response()
    await conversation.send_message(keyword_text)
    page = await conversation.get_response()
    classification = _jisou_page_classification(jisou, protocol_profile, page, _parse_buttons(page))
    return page, {
        "jisou_recovery_kind": "not_applicable",
        "reset_executed": False,
        "jisou_initial_page_phase": classification.page_phase,
        "jisou_bootstrap_kind": _jisou_bootstrap_kind(jisou, bootstrap_required),
    }


async def _has_jisou_conversation_history(client: Any, bot: str) -> bool:
    messages = await client.get_messages(bot, limit=1)
    return bool(messages)


def _jisou_bootstrap_kind(jisou: bool, bootstrap_required: bool) -> str:
    if not jisou:
        return "not_applicable"
    return "first_conversation" if bootstrap_required else "existing_conversation"


async def _execute_search_result_pages(
    client: Any,
    page: Any,
    bot: str,
    payload: dict[str, Any],
    target: dict[str, Any],
    *,
    protocol_profile: object,
    image_verification_solver: ImageVerificationSolver | None,
) -> dict[str, Any]:
    decoys: list[dict[str, Any]] = []
    total_results = 0
    page_no = 0
    jisou = is_jisou_bot(bot)
    page, selector_error, group_selector, selector_buttons = await _select_jisou_group_results_page(
        client,
        page,
        bot,
        protocol_profile,
        image_verification_solver=image_verification_solver,
    )
    if selector_error is not None:
        return selector_error
    while True:
        page_no += 1
        buttons = _parse_buttons(page)
        classification = _jisou_page_classification(jisou, protocol_profile, page, buttons)
        if jisou and classification.page_phase == VERIFICATION_IMAGE_PAGE:
            handled = await _handle_jisou_image_verification(
                client,
                bot,
                page,
                buttons,
                classification,
                image_verification_solver,
                protocol_profile=protocol_profile,
            )
            if handled.error is not None:
                return _with_search_protocol_trace(
                    handled.error,
                    buttons,
                    group_selector,
                    selector_buttons,
                    classification.approved_button_positions,
                    enabled=jisou,
                )
            page = handled.page
            buttons = _parse_buttons(page)
            classification = _jisou_page_classification(jisou, protocol_profile, page, buttons)
        if jisou and classification.page_phase != "group_result_page":
            return _jisou_result_page_error(classification, buttons, group_selector, selector_buttons)
        if not jisou and _human_verification_required(page):
            return _protocol_phase_error(
                "bot_human_verification_required",
                "搜索机器人要求人机验证，当前账号不能自动执行",
                ProtocolPageClassification("verification_page", frozenset(), frozenset()),
                buttons,
            )
        total_results += len(buttons)
        approved_positions = classification.approved_button_positions if jisou else None
        await _click_page_decoys(page, buttons, payload, target, decoys, approved_positions=approved_positions)
        text_match = _find_target_in_text(page, target)
        if text_match:
            if payload.get("search_execution_mode") == "click_only":
                return _failed(
                    "target_click_control_missing",
                    "目标仅以文本出现，没有可批准的纯点击控件",
                )
            result = await _execute_text_target_join(client, payload, target, text_match, decoys, page_no, total_results)
            traced = _with_search_protocol_trace(result, buttons, group_selector, selector_buttons, approved_positions, enabled=jisou)
            return _with_jisou_result_phase(traced, jisou)
        target_button = _find_target_button(buttons, target, approved_positions=approved_positions)
        if target_button:
            result = await _execute_target_join(client, page, payload, target, target_button, decoys, page_no, total_results)
            traced = _with_search_protocol_trace(result, buttons, group_selector, selector_buttons, approved_positions, enabled=jisou)
            return _with_jisou_result_phase(traced, jisou)
        next_button = _find_next_button(buttons, approved_positions=approved_positions)
        if next_button is None:
            result = _target_not_found(
                total_results,
                decoys,
                page_no,
                buttons,
                group_selector,
                selector_buttons,
                approved_positions,
                jisou,
            )
            return _with_jisou_result_phase(result, jisou)
        page = await _click_and_get_edited_page(client, bot, page, next_button)


def _with_jisou_result_phase(result: dict[str, Any], jisou: bool) -> dict[str, Any]:
    if not jisou:
        return result
    return {
        **result,
        "jisou_page_phase": str(result.get("jisou_page_phase") or "group_result_page"),
        "protocol_event_type": str(result.get("protocol_event_type") or "page_classified"),
    }


async def _select_jisou_group_results_page(
    client: Any,
    page: Any,
    bot_username: str,
    protocol_profile: object,
    *,
    image_verification_solver: ImageVerificationSolver | None = None,
) -> tuple[Any, dict[str, Any] | None, SearchJoinButton | None, list[SearchJoinButton]]:
    if not is_jisou_bot(bot_username):
        return page, None, None, []
    selector_buttons = _parse_buttons(page)
    classification = classify_jisou_page_with_media(
        profile=protocol_profile,
        message_text=_message_text(page),
        buttons=selector_buttons,
        has_photo=_has_photo_media(page),
    )
    phase = classification.page_phase
    if phase == VERIFICATION_IMAGE_PAGE:
        handled = await _handle_jisou_image_verification(
            client,
            bot_username,
            page,
            selector_buttons,
            classification,
            image_verification_solver,
            protocol_profile=protocol_profile,
        )
        if handled.error is not None:
            return page, handled.error, None, selector_buttons
        page = handled.page
        selector_buttons = _parse_buttons(page)
        classification = classify_jisou_page_with_media(
            profile=protocol_profile,
            message_text=_message_text(page),
            buttons=selector_buttons,
            has_photo=_has_photo_media(page),
        )
        phase = classification.page_phase
    if phase == "verification_page":
        return page, _protocol_phase_error(
            "bot_human_verification_required",
            "搜索机器人要求人机验证，当前账号不能自动执行",
            classification,
            selector_buttons,
        ), None, selector_buttons
    if phase == "hot_list_page":
        return page, _protocol_phase_error(
            "jisou_hot_list_page",
            "极搜关键词响应为热搜排行榜页，当前尝试失败并排除账号协议路径 12 小时",
            classification,
            selector_buttons,
        ), None, selector_buttons
    if phase == "group_result_page":
        return page, None, None, selector_buttons
    if phase != "search_category_page":
        return page, _protocol_phase_error(
            "jisou_session_state_deviated",
            "极搜关键词响应未匹配已知协议页面，账号会话状态偏离，排除 12 小时",
            classification,
            selector_buttons,
        ), None, selector_buttons
    group_button = _find_button_by_position(selector_buttons, classification.selector_positions)
    if group_button is None:
        return page, _selector_missing(selector_buttons, classification), None, selector_buttons
    result_page = await _click_and_get_edited_page(client, bot_username, page, group_button)
    result_buttons = _parse_buttons(result_page)
    result_classification = classify_jisou_page_with_media(
        profile=protocol_profile,
        message_text=_message_text(result_page),
        buttons=result_buttons,
        has_photo=_has_photo_media(result_page),
    )
    if result_classification.page_phase == VERIFICATION_IMAGE_PAGE:
        handled = await _handle_jisou_image_verification(
            client,
            bot_username,
            result_page,
            result_buttons,
            result_classification,
            image_verification_solver,
            protocol_profile=protocol_profile,
        )
        if handled.error is not None:
            return result_page, handled.error, group_button, selector_buttons
        result_page = handled.page
        result_buttons = _parse_buttons(result_page)
        result_classification = classify_jisou_page_with_media(
            profile=protocol_profile,
            message_text=_message_text(result_page),
            buttons=result_buttons,
            has_photo=_has_photo_media(result_page),
        )
    if result_classification.page_phase != "group_result_page":
        return result_page, _jisou_result_page_error(result_classification, result_buttons, group_button, selector_buttons), group_button, selector_buttons
    return result_page, None, group_button, selector_buttons


def _normalized_button_text(text: str) -> str:
    return normalize_visible_text(text)


def _selector_missing(selector_buttons: list[SearchJoinButton], classification: ProtocolPageClassification) -> dict[str, Any]:
    return {
        **_failed("jisou_group_selector_missing", "极搜群聊类型选择按钮缺失"),
        "jisou_page_phase": "search_category_page",
        "protocol_event_type": "page_classified",
        "search_protocol_trace": {"selector_page": _page_layout(selector_buttons, classification.approved_button_positions)},
    }


def _protocol_phase_error(
    code: str,
    detail: str,
    classification: ProtocolPageClassification,
    buttons: list[SearchJoinButton],
) -> dict[str, Any]:
    return {
        **_failed(code, detail),
        "jisou_page_phase": classification.page_phase,
        "protocol_event_type": "page_classified",
        "search_protocol_trace": {
            "page_phase": classification.page_phase,
            "page": _page_layout(buttons, classification.approved_button_positions),
        },
    }


def _jisou_page_classification(
    jisou: bool,
    protocol_profile: object,
    page: Any,
    buttons: list[SearchJoinButton],
) -> ProtocolPageClassification:
    if not jisou:
        return ProtocolPageClassification("", frozenset(), frozenset())
    return classify_jisou_page_with_media(
        profile=protocol_profile,
        message_text=_message_text(page),
        buttons=buttons,
        has_photo=_has_photo_media(page),
    )


def _has_photo_media(message: Any) -> bool:
    """PRD §2.19.1: 检测 telethon message 是否含 MessageMediaPhoto。"""
    media = getattr(message, "media", None)
    if media is None:
        return False
    return type(media).__name__ == "MessageMediaPhoto" or getattr(media, "photo", None) is not None


@dataclass(frozen=True)
class _ImageVerificationHandleResult:
    page: Any
    error: dict[str, Any] | None


async def _handle_jisou_image_verification(
    client: Any,
    bot_username: str,
    page: Any,
    buttons: list[SearchJoinButton],
    classification: ProtocolPageClassification,
    solver: ImageVerificationSolver | None,
    *,
    protocol_profile: object,
) -> _ImageVerificationHandleResult:
    """Handle one immutable challenge fingerprint and one approved submit."""
    digit_buttons = [button for button in buttons if _is_digit_callback_button(button)]
    candidate_answers = [button.text.strip() for button in digit_buttons]
    fingerprint = _image_verification_fingerprint(page, buttons)
    if solver is None or not candidate_answers:
        return _image_verification_required_result(
            classification, buttons, fingerprint, "verification_ai_unavailable"
        )
    image_bytes = await _download_verification_image(client, page)
    if not image_bytes:
        return _image_verification_required_result(
            classification, buttons, fingerprint, "verification_transport_unavailable"
        )
    mime_type = _message_media_mime_type(page)
    try:
        solved = await asyncio.to_thread(
            solver,
            image_bytes,
            mime_type,
            candidate_answers,
        )
    except ImageVerificationProviderUnavailableError as exc:
        return _image_verification_required_result(
            classification,
            buttons,
            fingerprint,
            "verification_ai_unavailable",
            detail=str(exc),
        )
    except ImageVerificationNoSafeAnswerError as exc:
        return _image_verification_failed_result(
            classification,
            buttons,
            str(exc),
            fingerprint=fingerprint,
        )
    if solved is None:
        return _image_verification_failed_result(
            classification,
            buttons,
            "no healthy provider returned a safe answer",
            fingerprint=fingerprint,
        )
    answer, confidence = solved
    if confidence < IMAGE_VERIFICATION_MIN_CONFIDENCE:
        return _image_verification_failed_result(
            classification, buttons, f"confidence {confidence:.2f} below threshold {IMAGE_VERIFICATION_MIN_CONFIDENCE}",
            answer=answer, confidence=confidence, fingerprint=fingerprint,
        )
    target_button = next((button for button in digit_buttons if button.text.strip() == answer), None)
    if target_button is None:
        return _image_verification_failed_result(
            classification, buttons, f"answer {answer} not in button matrix",
            answer=answer, confidence=confidence, fingerprint=fingerprint,
        )
    clicked_page = await _click_and_get_edited_page(client, bot_username, page, target_button)
    clicked_buttons = _parse_buttons(clicked_page)
    clicked_classification = classify_jisou_page_with_media(
        profile=protocol_profile,
        message_text=_message_text(clicked_page),
        buttons=clicked_buttons,
        has_photo=_has_photo_media(clicked_page),
    )
    if clicked_classification.page_phase == VERIFICATION_IMAGE_PAGE:
        next_fingerprint = _image_verification_fingerprint(clicked_page, clicked_buttons)
        if next_fingerprint == fingerprint:
            return _image_verification_failed_result(
                clicked_classification,
                clicked_buttons,
                "remote explicitly rejected approved answer",
                answer=answer,
                confidence=confidence,
                fingerprint=fingerprint,
            )
        return _image_verification_required_result(
            clicked_classification,
            clicked_buttons,
            next_fingerprint,
            "new_challenge_fingerprint",
        )
    return _ImageVerificationHandleResult(page=clicked_page, error=None)


def _image_verification_fingerprint(
    page: Any,
    buttons: list[SearchJoinButton],
) -> str:
    message_id = str(getattr(page, "id", "") or "")
    callback_fingerprint = [
        (button.row, button.col, button.text, button.button_type)
        for button in buttons
    ]
    value = repr((message_id, callback_fingerprint)).encode()
    return hashlib.sha256(value).hexdigest()


async def _download_verification_image(client: Any, page: Any) -> bytes:
    try:
        data = await client.download_media(page, file=bytes)
    except Exception:  # noqa: BLE001 - 下载失败按空字节处理，由调用方写 failed。
        return b""
    return bytes(data) if data else b""


def _message_media_mime_type(message: Any) -> str:
    media = getattr(message, "media", None)
    for candidate in [media, getattr(media, "document", None), getattr(media, "photo", None)]:
        mime_type = getattr(candidate, "mime_type", None)
        if mime_type:
            return str(mime_type)
    return "image/png"


def _is_digit_callback_button(button: SearchJoinButton) -> bool:
    if button.button_type != "callback_data":
        return False
    text = button.text.strip()
    return bool(text) and text.isdigit()


def _image_verification_failed_result(
    classification: ProtocolPageClassification,
    buttons: list[SearchJoinButton],
    detail: str,
    *,
    answer: str = "",
    confidence: float = 0.0,
    fingerprint: str = "",
) -> _ImageVerificationHandleResult:
    """PRD §2.19.2 第 5 步：识别失败、置信度不足、answer 不在矩阵、重试仍空都写 jisou_image_verification_failed。"""
    error = {
        **_failed("jisou_image_verification_failed", f"极搜图片算式验证码识别失败：{detail}"),
        "jisou_page_phase": VERIFICATION_IMAGE_PAGE,
        "protocol_event_type": "image_verification_failed",
        "image_verification_answer": answer,
        "image_verification_confidence": confidence,
        "image_verification_status": "failed",
        "image_verification_detail": detail,
        "challenge_fingerprint_hash": fingerprint,
        "search_protocol_trace": {
            "page_phase": VERIFICATION_IMAGE_PAGE,
            "page": _page_layout(buttons, classification.approved_button_positions),
        },
    }
    return _ImageVerificationHandleResult(page=None, error=error)


def _image_verification_required_result(
    classification: ProtocolPageClassification,
    buttons: list[SearchJoinButton],
    fingerprint: str,
    reason: str,
    *,
    detail: str = "",
) -> _ImageVerificationHandleResult:
    error = {
        **_failed("jisou_image_verification_required", "极搜图片验证码等待安全识别结果"),
        "jisou_page_phase": VERIFICATION_IMAGE_PAGE,
        "protocol_event_type": "image_verification_required",
        "image_verification_status": "required",
        "image_verification_reason": reason,
        "image_verification_detail": detail,
        "challenge_fingerprint_hash": fingerprint,
        "search_protocol_trace": {
            "page_phase": VERIFICATION_IMAGE_PAGE,
            "page": _page_layout(buttons, classification.approved_button_positions),
        },
    }
    return _ImageVerificationHandleResult(page=None, error=error)


def _jisou_result_page_error(
    classification: ProtocolPageClassification,
    buttons: list[SearchJoinButton],
    group_selector: SearchJoinButton | None,
    selector_buttons: list[SearchJoinButton],
) -> dict[str, Any]:
    if classification.page_phase == "verification_page":
        result = _protocol_phase_error("bot_human_verification_required", "极搜结果页要求人机验证，当前账号不能自动执行", classification, buttons)
    else:
        result = _protocol_phase_error("jisou_session_state_deviated", "极搜已进入群聊结果路径后页面偏离已审批协议", classification, buttons)
    return _with_search_protocol_trace(result, buttons, group_selector, selector_buttons, classification.approved_button_positions)


def _target_not_found(
    total: int,
    decoys: list[dict[str, Any]],
    page_no: int,
    buttons: list[SearchJoinButton],
    group_selector: SearchJoinButton | None,
    selector_buttons: list[SearchJoinButton],
    approved_positions: frozenset[int] | None,
    jisou: bool,
) -> dict[str, Any]:
    return {
        **_failed("target_not_in_results", "目标群未出现在搜索结果"),
        "total_results": total,
        "pre_join_decoy_clicks": decoys,
        "page": page_no,
        "searched_pages": page_no,
        "last_result_page": page_no,
        "search_end_reason": "no_next_page",
        **_search_protocol_trace(buttons, group_selector, selector_buttons, approved_positions, enabled=jisou),
    }


def _search_protocol_trace(
    buttons: list[SearchJoinButton],
    group_selector: SearchJoinButton | None,
    selector_buttons: list[SearchJoinButton],
    approved_positions: frozenset[int] | None,
    *,
    enabled: bool,
) -> dict[str, Any]:
    if not enabled:
        return {}
    selector_positions = frozenset({group_selector.position}) if group_selector is not None else frozenset()
    trace = {"result_page": _page_layout(buttons, approved_positions)}
    if group_selector is not None:
        trace["jisou_group_selector"] = {
            "position": group_selector.position,
            "text_hash": _button_hash(group_selector),
            "text_length": len(group_selector.text),
            "approved_sample_match": True,
        }
        trace["selector_page"] = _page_layout(selector_buttons, selector_positions)
    return {
        "search_protocol_trace": trace
    }


def _with_search_protocol_trace(
    result: dict[str, Any],
    buttons: list[SearchJoinButton],
    group_selector: SearchJoinButton | None,
    selector_buttons: list[SearchJoinButton],
    approved_positions: frozenset[int] | None,
    *,
    enabled: bool = True,
) -> dict[str, Any]:
    trace = _search_protocol_trace(buttons, group_selector, selector_buttons, approved_positions, enabled=enabled)
    return result if not trace else {**result, **trace}


def _page_layout(buttons: list[SearchJoinButton], approved_positions: frozenset[int] | None = None) -> dict[str, Any]:
    approved = approved_positions or frozenset()
    return {"button_count": len(buttons), "button_layout": [_button_layout(button, approved) for button in buttons]}


def _button_layout(button: SearchJoinButton, approved_positions: frozenset[int]) -> dict[str, Any]:
    normalized = _normalized_button_text(button.text)
    return {
        "row": button.row,
        "col": button.col,
        "button_type": button.button_type,
        "effect": button.effect,
        # PRD §2.19.4 观测盲点修复：把已计算的 normalized_text 写入 trace，
        # 便于失败回放（脱敏后的归一化文案，不持久化机器人原文）。
        "normalized_text": normalized,
        "url": button.url,
        "position": button.position,
        "text_length": len(button.text),
        "contains_page_marker": any(marker in normalized for marker in NAVIGATION_MARKERS),
        "navigation_symbols": [name for symbol, name in PAGINATION_SYMBOL_NAMES.items() if symbol in button.text],
        "approved_sample_match": button.position in approved_positions,
    }


async def _click_page_decoys(
    page: Any,
    buttons: list[SearchJoinButton],
    payload: dict[str, Any],
    target: dict[str, Any],
    decoys: list[dict[str, Any]],
    *,
    approved_positions: frozenset[int] | None,
) -> None:
    limit = int(_safe(payload).get("pre_join_decoy_click_max") or 0)
    if len(decoys) >= limit:
        return
    clicked = await _click_safe_navigation(page, buttons, target, limit - len(decoys), approved_positions=approved_positions)
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
    if (
        payload.get("search_execution_mode") == "click_only"
        and button.effect not in {"navigate_only", "target_open_only"}
    ):
        return _failed(
            "membership_side_effect_not_allowed",
            "纯搜索点击禁止执行可能改变成员关系的控件",
        )
    await _click_button(page, button)
    return {
        **_success(payload, button, total, decoys, page_no),
        "target_message_id": str(getattr(page, "id", "") or ""),
        "target_username": str(target.get("username") or ""),
        "bot_username": _bot_username(payload),
    }


async def _execute_text_target_join(
    client: Any,
    payload: dict[str, Any],
    target: dict[str, Any],
    match: TextTargetMatch,
    decoys: list[dict[str, Any]],
    page_no: int,
    total: int,
) -> dict[str, Any]:
    return {
        **_success(payload, None, total, decoys, page_no),
        "target_position": match.position,
        "target_match_source": match.source,
        "target_line_hash": _text_hash(match.line),
        "target_line_length": len(match.line),
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
    if _is_navigation_text(text) or _is_next_page_text(text):
        return "navigate_only"
    if url and (urlparse(url).netloc or "").lower() not in TELEGRAM_HOSTS:
        return "external"
    if url:
        return "target_open_only"
    return "unknown"


async def _click_safe_navigation(
    message: Any,
    buttons: list[SearchJoinButton],
    target: dict[str, Any],
    limit: int,
    *,
    approved_positions: frozenset[int] | None,
) -> list[dict[str, Any]]:
    clicked: list[dict[str, Any]] = []
    for button in buttons:
        if len(clicked) >= limit:
            break
        if approved_positions is not None and button.position not in approved_positions:
            continue
        if button.effect != "navigate_only" or _is_page_nav_button(button) or _matches_target(button, target):
            continue
        await _click_button(message, button)
        clicked.append({"position": button.position, "button_hash": _button_hash(button), "effect": button.effect, "joined": False})
    return clicked


def _find_target_button(
    buttons: list[SearchJoinButton],
    target: dict[str, Any],
    *,
    approved_positions: frozenset[int] | None,
) -> SearchJoinButton | None:
    for button in buttons:
        if approved_positions is not None and button.position not in approved_positions:
            continue
        if _matches_target(button, target):
            return button
    return None


def _find_next_button(
    buttons: list[SearchJoinButton],
    *,
    approved_positions: frozenset[int] | None,
) -> SearchJoinButton | None:
    for button in buttons:
        if approved_positions is not None and button.position not in approved_positions:
            continue
        if _is_next_page_button(button):
            return button
    return None


def _is_next_page_button(button: SearchJoinButton) -> bool:
    if button.button_type != "callback_data":
        return False
    return _is_next_page_text(button.text)


def _is_next_page_text(value: str) -> bool:
    text = _normalized_button_text(value)
    if "下一页" in text or "next" in text:
        return True
    symbols = text.replace(VARIATION_SELECTOR, "")
    return bool(symbols) and all(symbol in NEXT_PAGE_SYMBOLS for symbol in symbols)


def _matches_target(button: SearchJoinButton, target: dict[str, Any]) -> bool:
    username = str(target.get("username") or "").strip().lower().lstrip("@")
    if username and button.target_username.lower() == username:
        return True
    return False


def _find_button_by_position(buttons: list[SearchJoinButton], positions: frozenset[int]) -> SearchJoinButton | None:
    return next((button for button in buttons if button.position in positions), None)


async def _click_button(message: Any, button: SearchJoinButton) -> Any:
    return await message.click(button.row, button.col)


async def _click_and_get_edited_page(client: Any, bot_username: str, message: Any, button: SearchJoinButton) -> Any:
    await _click_button(message, button)
    edited_page = await client.get_messages(bot_username, ids=message.id)
    if edited_page is not None:
        return edited_page
    newer_page = await _latest_newer_bot_message(client, bot_username, message)
    if newer_page is None:
        raise RuntimeError("callback edited message unavailable")
    return newer_page


async def _latest_newer_bot_message(client: Any, bot_username: str, message: Any) -> Any | None:
    latest = await client.get_messages(bot_username, limit=1)
    candidate = latest[0] if isinstance(latest, (list, tuple)) and latest else latest
    if candidate is None:
        return None
    current_id = int(getattr(message, "id", 0) or 0)
    candidate_id = int(getattr(candidate, "id", 0) or 0)
    return candidate if current_id > 0 and candidate_id > current_id else None


async def _join_channel(client: Any, entity: Any) -> None:
    from telethon import functions

    try:
        await client(functions.channels.JoinChannelRequest(channel=entity))
    except Exception as exc:
        if _is_already_participant_error(exc):
            return
        if _is_join_request_pending_error(exc):
            raise _JoinRequestPendingError from exc
        raise


async def _assert_current_account_is_member(client: Any, entity: Any) -> None:
    from telethon import functions

    try:
        current_account = await client.get_me(input_peer=True)
        await client(functions.channels.GetParticipantRequest(channel=entity, participant=current_account))
    except Exception as exc:
        if _is_not_participant_error(exc):
            raise _MembershipNotObservedError from exc
        raise


def _external_blocked(button: SearchJoinButton, total: int, decoys: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        **_failed("external_url_requires_web_profile", "外部 HTTP URL 需要 Web Profile，首版不执行"),
        "target_position": button.position,
        "total_results": total,
        "pre_join_decoy_clicks": decoys,
    }


def _success(payload: dict[str, Any], button: SearchJoinButton | None, total: int, decoys: list[dict[str, Any]], page_no: int) -> dict[str, Any]:
    result = {
        "success": True,
        "join_status": "target_found",
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
    if payload.get("search_execution_mode") == "click_only" and button is not None:
        result.update({
            "target_click_observed": True,
            "membership_side_effect": "none",
            "membership_mutating_rpc_invoked": False,
            "target_button_type": button.button_type,
            "target_button_effect": button.effect,
            "target_button_row": button.row,
            "target_button_col": button.col,
            "target_button_fingerprint": _button_hash(button),
        })
    return result


def _membership_observed_result(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "success": True,
        "join_status": "membership_observed",
        "membership_observed": True,
        "target_group_id": payload.get("target_group_id"),
        "target_peer_id": payload.get("target_peer_id"),
        "post_join_policy": payload.get("post_join_policy") or "stay_joined",
    }


def _membership_not_observed(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "success": False,
        "error_code": "membership_not_observed",
        "detail": "入群申请仍未观察到成员关系",
        "join_status": "membership_pending",
        "target_group_id": payload.get("target_group_id"),
    }


def _join_request_pending(target_result: dict[str, Any]) -> dict[str, Any]:
    pending_result = dict(target_result)
    pending_result.pop("membership_observed", None)
    pending_result.pop("membership_observed_at", None)
    return {
        **pending_result,
        "success": False,
        "error_code": "join_request_pending",
        "detail": "目标群开启入群审批，已提交申请但尚未观察到成员关系",
        "join_status": "join_request_pending",
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
    title = _normalized_target_title(target.get("title"))
    pattern = re.compile(rf"(?<![a-z0-9_])@?{re.escape(username)}(?![a-z0-9_])")
    for position, line in enumerate(_message_text(message).splitlines(), start=1):
        normalized = line.strip().lower()
        if not normalized:
            continue
        if pattern.search(normalized):
            return TextTargetMatch(position, line.strip(), "message_text")
        if _line_has_exact_target_title(line, title):
            return TextTargetMatch(position, line.strip(), "message_title_username_verified")
    return None


def _line_has_exact_target_title(line: str, title: str) -> bool:
    normalized_line = _normalized_target_title(line)
    position = normalized_line.find(title)
    while position >= 0:
        before = normalized_line[position - 1] if position else ""
        end = position + len(title)
        after = normalized_line[end] if end < len(normalized_line) else ""
        if not _is_title_name_character(before) and not _is_title_name_character(after):
            return True
        position = normalized_line.find(title, position + 1)
    return False


def _normalized_target_title(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(char for char in text if not char.isspace() and char != VARIATION_SELECTOR)


def _is_title_name_character(value: str) -> bool:
    return bool(value) and (value.isalnum() or value == "_")


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


def _target_join_ref(target: dict[str, Any]) -> str:
    return str(target.get("username") or target.get("group_id") or "").strip()


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


def _is_already_participant_error(exc: Exception) -> bool:
    text = f"{exc.__class__.__name__} {exc}".lower()
    return "already" in text and ("participant" in text or "member" in text)


def _is_join_request_pending_error(exc: Exception) -> bool:
    text = f"{exc.__class__.__name__} {exc}".lower()
    return "requested to join this chat or channel" in text


def _is_not_participant_error(exc: Exception) -> bool:
    text = f"{exc.__class__.__name__} {exc}".lower()
    return "notparticipant" in text or "not a participant" in text


def _is_navigation_text(text: str) -> bool:
    normalized = text.strip().lower()
    return any(marker in normalized for marker in NAVIGATION_MARKERS)


def _is_page_nav_button(button: SearchJoinButton) -> bool:
    text = button.text.lower()
    return _is_next_page_text(button.text) or "上一页" in text or "prev" in text


def _button_hash(button: SearchJoinButton) -> str:
    raw = f"{button.text}:{button.url}:{button.position}"
    return _text_hash(raw)[:16]


def _text_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


__all__ = [
    "ImageVerificationNoSafeAnswerError",
    "ImageVerificationProviderUnavailableError",
    "ImageVerificationSolver",
    "SearchJoinButton",
    "execute_search_join_with_client",
]

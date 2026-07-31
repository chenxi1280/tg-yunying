from __future__ import annotations

import asyncio
import hashlib
import re
import unicodedata
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from time import monotonic
from typing import Any, Callable
from urllib.parse import urlparse

from app.search_join_protocol import (
    VERIFICATION_IMAGE_PAGE,
    ProtocolPageClassification,
    classify_jisou_page_with_media,
    is_jisou_bot,
    normalize_visible_text,
)

from .search_join_entity_results import (
    SearchResultEntityLink,
    approved_navigation_positions,
    find_target_entity_link,
    is_group_result_entity_page,
    open_target_entity,
    search_result_entity_links,
    target_entity_fingerprint,
)


TELEGRAM_HOSTS = {"t.me", "telegram.me", "www.t.me", "www.telegram.me"}
NAVIGATION_MARKERS = ("下一页", "上一页", "next", "prev", "page", "页")
HUMAN_VERIFICATION_MARKERS = ("人机验证", "计算结果", "captcha")

# PRD §2.19.2 图片验证码识别相关常量。
IMAGE_VERIFICATION_MIN_CONFIDENCE = 0.70
VERIFICATION_ANSWER_PATTERN = re.compile(r"^[A-Za-z0-9]+$")
CALLBACK_PAGE_RESPONSE_TIMEOUT_SECONDS = 8.0
CALLBACK_PAGE_POLL_INTERVAL_SECONDS = 0.5
DEFAULT_IMAGE_VERIFICATION_CHALLENGE_LIMIT = 3
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
class ImageVerificationRequest:
    image_bytes: bytes
    mime_type: str
    candidate_answers: tuple[str, ...]
    challenge_text: str
    challenge_fingerprint_hash: str = ""
    message_id: str = ""
    message_revision: str = ""
    bot_peer_hash: str = ""
    image_hash: str = ""
    candidate_hash: str = ""
    challenge_observed_at: datetime | None = None
    challenge_observed_monotonic: float | None = None


@dataclass(frozen=True)
class ImageVerificationVote:
    source: str
    status: str
    answer: str = ""
    confidence: float = 0.0
    in_candidates: bool = False
    detail: str = ""
    started_at: str = ""
    completed_at: str = ""
    duration_ms: int = 0
    late: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "status": self.status,
            "answer": self.answer,
            "confidence": round(self.confidence, 4),
            "in_candidates": self.in_candidates,
            "detail": self.detail,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_ms": self.duration_ms,
            "late": self.late,
        }


@dataclass(frozen=True)
class ImageVerificationDecision:
    answer: str
    confidence: float
    votes: tuple[ImageVerificationVote, ...]
    model_waited: bool = True
    model_started: bool = True
    model_start_reason: str = ""
    consensus_source: str = ""
    contract_version: str = ""
    challenge_observed_at: str = ""
    model_hedge_at: str = ""
    callback_submit_deadline: str = ""
    callback_submit_deadline_monotonic: float = 0.0


ImageVerificationSolver = Callable[
    [ImageVerificationRequest],
    "ImageVerificationDecision | None",
]


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


class ImageVerificationConsensusUnavailableError(RuntimeError):
    def __init__(self, detail: str, votes: tuple[ImageVerificationVote, ...]):
        super().__init__(detail)
        self.votes = votes


class ImageVerificationRuntimeContractError(RuntimeError):
    def __init__(
        self,
        code: str,
        detail: str,
        votes: tuple[ImageVerificationVote, ...] = (),
        callback_submit_deadline_monotonic: float = 0.0,
    ) -> None:
        super().__init__(detail)
        self.code = code
        self.votes = votes
        self.callback_submit_deadline_monotonic = (
            callback_submit_deadline_monotonic
        )


class _VerificationCallbackDeadlineExceeded(RuntimeError):
    pass


class _VerificationCallbackResultUnknown(RuntimeError):
    pass


async def execute_search_join_with_client(
    client: Any,
    payload: dict[str, Any],
    *,
    keyword_text: str,
    image_verification_solver: ImageVerificationSolver | None = None,
    image_verification_challenge_limit: int = (
        DEFAULT_IMAGE_VERIFICATION_CHALLENGE_LIMIT
    ),
    image_verification_callback_unknown_fingerprints: frozenset[str] = frozenset(),
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
            image_verification_challenge_limit=(
                image_verification_challenge_limit
            ),
            callback_unknown_fingerprints=(
                image_verification_callback_unknown_fingerprints
            ),
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
    image_verification_challenge_limit: int,
    callback_unknown_fingerprints: frozenset[str],
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
            conversation=conv,
            keyword_text=keyword_text,
            protocol_profile=protocol_profile,
            image_verification_solver=image_verification_solver,
            verification_budget=_ImageVerificationBudget(
                limit=max(1, image_verification_challenge_limit),
            ),
            callback_unknown_fingerprints=callback_unknown_fingerprints,
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
    conversation: Any,
    keyword_text: str,
    protocol_profile: object,
    image_verification_solver: ImageVerificationSolver | None,
    verification_budget: "_ImageVerificationBudget",
    callback_unknown_fingerprints: frozenset[str],
) -> dict[str, Any]:
    decoys: list[dict[str, Any]] = []
    total_results = 0
    page_no = 0
    jisou = is_jisou_bot(bot)
    page, selector_error, group_selector, selector_buttons, verification_attempts = await _select_jisou_group_results_page(
        client,
        page,
        bot,
        conversation=conversation,
        keyword_text=keyword_text,
        protocol_profile=protocol_profile,
        image_verification_solver=image_verification_solver,
        verification_budget=verification_budget,
        callback_unknown_fingerprints=callback_unknown_fingerprints,
    )
    if selector_error is not None:
        return _with_no_post_verification_replay(selector_error)
    while True:
        page_no += 1
        buttons = _parse_buttons(page)
        classification = _jisou_page_classification(jisou, protocol_profile, page, buttons)
        if jisou and classification.page_phase == VERIFICATION_IMAGE_PAGE:
            if not verification_budget.consume():
                return _with_image_verification_attempts(
                    _image_verification_budget_exhausted(
                        classification, buttons
                    ).error,
                    verification_attempts,
                )
            handled = await _handle_jisou_image_verification(
                client,
                bot,
                page,
                buttons,
                classification,
                image_verification_solver,
                protocol_profile=protocol_profile,
                callback_unknown_fingerprints=callback_unknown_fingerprints,
            )
            if handled.audit is not None:
                verification_attempts.append(handled.audit)
            refreshed = await _refresh_unresolved_verification(
                _JisouNavigationContext(
                    client=client,
                    bot_username=bot,
                    conversation=conversation,
                    keyword_text=keyword_text,
                    protocol_profile=protocol_profile,
                    image_verification_solver=image_verification_solver,
                    verification_budget=verification_budget,
                    callback_unknown_fingerprints=callback_unknown_fingerprints,
                ),
                page,
                handled,
            )
            if refreshed is not None:
                if refreshed.audit is not None:
                    verification_attempts.append(refreshed.audit)
                handled = refreshed
            next_challenge = _new_verification_challenge_page(handled)
            if next_challenge is not None:
                page = next_challenge
                continue
            if handled.error is not None:
                traced_error = _with_search_protocol_trace(
                    handled.error,
                    buttons,
                    group_selector,
                    selector_buttons,
                    classification.approved_button_positions,
                    enabled=jisou,
                )
                return _with_image_verification_attempts(
                    traced_error, verification_attempts
                )
            page = handled.page
            buttons = _parse_buttons(page)
            classification = _jisou_page_classification(jisou, protocol_profile, page, buttons)
        if jisou and classification.page_phase != "group_result_page":
            return _with_image_verification_attempts(
                _jisou_result_page_error(
                    classification, buttons, group_selector, selector_buttons
                ),
                verification_attempts,
            )
        if not jisou and _human_verification_required(page):
            return _protocol_phase_error(
                "bot_human_verification_required",
                "搜索机器人要求人机验证，当前账号不能自动执行",
                ProtocolPageClassification("verification_page", frozenset(), frozenset()),
                buttons,
            )
        entity_links = search_result_entity_links(page)
        total_results += len(entity_links) if entity_links else len(buttons)
        approved_positions = classification.approved_button_positions if jisou else None
        if not entity_links:
            await _click_page_decoys(
                page,
                buttons,
                payload,
                target,
                decoys,
                approved_positions=approved_positions,
            )
        entity_target = find_target_entity_link(
            entity_links,
            str(target.get("username") or ""),
        )
        if entity_target is not None:
            result = await _execute_target_entity_open(
                _TargetEntityOpenContext(
                    client=client,
                    page=page,
                    payload=payload,
                    target=target,
                    decoys=decoys,
                    page_no=page_no,
                    total=total_results,
                ),
                entity_target,
            )
            traced = _with_search_protocol_trace(
                result,
                buttons,
                group_selector,
                selector_buttons,
                approved_positions,
                enabled=jisou,
            )
            return _with_jisou_result_phase(
                _with_image_verification_attempts(traced, verification_attempts),
                jisou,
            )
        text_match = _find_target_in_text(page, target)
        if text_match:
            if payload.get("search_execution_mode") == "click_only":
                return _with_image_verification_attempts(
                    _failed(
                        "target_click_control_missing",
                        "目标仅以文本出现，没有可批准的纯点击控件",
                    ),
                    verification_attempts,
                )
            result = await _execute_text_target_join(client, payload, target, text_match, decoys, page_no, total_results)
            traced = _with_search_protocol_trace(result, buttons, group_selector, selector_buttons, approved_positions, enabled=jisou)
            return _with_jisou_result_phase(
                _with_image_verification_attempts(traced, verification_attempts),
                jisou,
            )
        target_button = _find_target_button(buttons, target, approved_positions=approved_positions)
        if target_button:
            result = await _execute_target_join(client, page, payload, target, target_button, decoys, page_no, total_results)
            traced = _with_search_protocol_trace(result, buttons, group_selector, selector_buttons, approved_positions, enabled=jisou)
            return _with_jisou_result_phase(
                _with_image_verification_attempts(traced, verification_attempts),
                jisou,
            )
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
            return _with_jisou_result_phase(
                _with_image_verification_attempts(result, verification_attempts),
                jisou,
            )
        page = await _click_and_get_edited_page(client, bot, page, next_button)


def _with_jisou_result_phase(
    result: dict[str, Any],
    jisou: bool,
) -> dict[str, Any]:
    if not jisou:
        return result
    return {
        **result,
        "jisou_page_phase": str(result.get("jisou_page_phase") or "group_result_page"),
        "protocol_event_type": str(result.get("protocol_event_type") or "page_classified"),
        "jisou_post_verification_keyword_replayed": False,
    }


def _with_no_post_verification_replay(
    result: dict[str, Any],
) -> dict[str, Any]:
    return {
        **result,
        "jisou_post_verification_keyword_replayed": False,
    }


async def _select_jisou_group_results_page(
    client: Any,
    page: Any,
    bot_username: str,
    *,
    conversation: Any,
    keyword_text: str,
    protocol_profile: object,
    image_verification_solver: ImageVerificationSolver | None = None,
    verification_budget: "_ImageVerificationBudget",
    callback_unknown_fingerprints: frozenset[str],
) -> tuple[Any, dict[str, Any] | None, SearchJoinButton | None, list[SearchJoinButton], list[dict[str, Any]]]:
    if not is_jisou_bot(bot_username):
        return page, None, None, [], []
    context = _JisouNavigationContext(
        client=client,
        bot_username=bot_username,
        conversation=conversation,
        keyword_text=keyword_text,
        protocol_profile=protocol_profile,
        image_verification_solver=image_verification_solver,
        verification_budget=verification_budget,
        callback_unknown_fingerprints=callback_unknown_fingerprints,
    )
    return await _navigate_jisou_to_group_results(context, page)


@dataclass(frozen=True)
class _JisouNavigationContext:
    client: Any
    bot_username: str
    conversation: Any
    keyword_text: str
    protocol_profile: object
    image_verification_solver: ImageVerificationSolver | None
    verification_budget: "_ImageVerificationBudget"
    callback_unknown_fingerprints: frozenset[str]


@dataclass
class _ImageVerificationBudget:
    limit: int
    challenge_count: int = 0
    refresh_count: int = 0

    def consume(self) -> bool:
        if self.challenge_count >= self.limit:
            return False
        self.challenge_count += 1
        return True


async def _navigate_jisou_to_group_results(
    context: _JisouNavigationContext,
    page: Any,
) -> tuple[
    Any,
    dict[str, Any] | None,
    SearchJoinButton | None,
    list[SearchJoinButton],
    list[dict[str, Any]],
]:
    group_selector: SearchJoinButton | None = None
    selector_buttons: list[SearchJoinButton] = []
    verification_attempts: list[dict[str, Any]] = []
    while True:
        buttons, classification = _classify_jisou_navigation_page(
            page, context.protocol_profile
        )
        if group_selector is None:
            selector_buttons = buttons
        phase = classification.page_phase
        if phase == VERIFICATION_IMAGE_PAGE:
            if not context.verification_budget.consume():
                exhausted = _image_verification_budget_exhausted(
                    classification, buttons
                )
                return (
                    page,
                    _with_image_verification_attempts(
                        exhausted.error or {}, verification_attempts
                    ),
                    group_selector,
                    selector_buttons,
                    verification_attempts,
                )
            handled = await _handle_navigation_verification(
                context, page, buttons, classification
            )
            if handled.audit is not None:
                verification_attempts.append(handled.audit)
            refreshed = await _refresh_unresolved_verification(
                context,
                page,
                handled,
            )
            if refreshed is not None:
                if refreshed.audit is not None:
                    verification_attempts.append(refreshed.audit)
                handled = refreshed
            next_challenge = _new_verification_challenge_page(handled)
            if next_challenge is not None:
                page = next_challenge
                continue
            if handled.error is not None:
                return (
                    page,
                    _with_image_verification_attempts(
                        handled.error, verification_attempts
                    ),
                    group_selector,
                    selector_buttons,
                    verification_attempts,
                )
            page = handled.page
            continue
        if phase == "group_result_page":
            return (
                page,
                None,
                group_selector,
                selector_buttons,
                verification_attempts,
            )
        group_selector = _navigation_group_selector(
            classification,
            buttons,
        )
        if group_selector is not None:
            selector_buttons = buttons
            page = await _click_and_get_edited_page(
                context.client,
                context.bot_username,
                page,
                group_selector,
            )
            continue
        error = _jisou_navigation_phase_error(classification, buttons)
        if error is not None:
            return page, error, group_selector, selector_buttons, verification_attempts
        if group_selector is None:
            return (
                page,
                _selector_missing(buttons, classification),
                None,
                buttons,
                verification_attempts,
            )


async def _handle_navigation_verification(
    context: _JisouNavigationContext,
    page: Any,
    buttons: list[SearchJoinButton],
    classification: ProtocolPageClassification,
) -> _ImageVerificationHandleResult:
    return await _handle_jisou_image_verification(
        context.client,
        context.bot_username,
        page,
        buttons,
        classification,
        context.image_verification_solver,
        protocol_profile=context.protocol_profile,
        callback_unknown_fingerprints=context.callback_unknown_fingerprints,
    )


async def _refresh_unresolved_verification(
    context: _JisouNavigationContext,
    page: Any,
    handled: _ImageVerificationHandleResult,
) -> _ImageVerificationHandleResult | None:
    error = handled.error or {}
    if error.get("image_verification_reason") != (
        "verification_consensus_unavailable"
    ):
        return None
    if context.verification_budget.challenge_count >= (
        context.verification_budget.limit
    ):
        buttons, classification = _classify_jisou_navigation_page(
            page, context.protocol_profile
        )
        return _image_verification_budget_exhausted(
            classification, buttons
        )
    try:
        await context.conversation.send_message(context.keyword_text)
        refreshed_page = await context.conversation.get_response()
    except Exception as exc:  # noqa: BLE001 - persisted as explicit protocol fact.
        return _image_verification_required_result(
            _classify_jisou_navigation_page(
                page, context.protocol_profile
            )[1],
            _parse_buttons(page),
            str(error.get("challenge_fingerprint_hash") or ""),
            "verification_refresh_transport_unavailable",
            detail=str(exc) or exc.__class__.__name__,
        )
    context.verification_budget.refresh_count += 1
    return await _validate_refreshed_verification_page(
        context,
        refreshed_page,
        str(error.get("challenge_fingerprint_hash") or ""),
    )


async def _validate_refreshed_verification_page(
    context: _JisouNavigationContext,
    page: Any,
    previous_fingerprint: str,
) -> _ImageVerificationHandleResult:
    buttons, classification = _classify_jisou_navigation_page(
        page, context.protocol_profile
    )
    if classification.page_phase != VERIFICATION_IMAGE_PAGE:
        return _image_verification_required_result(
            classification,
            buttons,
            previous_fingerprint,
            "verification_refresh_unexpected_page",
            page=page,
        )
    image_bytes = await _download_verification_image(context.client, page)
    fingerprint = _image_verification_fingerprint(
        page, buttons, image_bytes
    )
    if not image_bytes:
        return _image_verification_required_result(
            classification,
            buttons,
            fingerprint,
            "verification_transport_unavailable",
            page=page,
        )
    if fingerprint == previous_fingerprint:
        return _image_verification_failed_result(
            classification,
            buttons,
            "keyword refresh returned the same challenge fingerprint",
            fingerprint=fingerprint,
        )
    audit = {
        "status": "consensus_unavailable_keyword_refresh",
        "challenge_fingerprint_hash": previous_fingerprint,
        "next_challenge_fingerprint_hash": fingerprint,
        "refresh_kind": "keyword_replay",
    }
    required = _image_verification_required_result(
        classification,
        buttons,
        fingerprint,
        "new_challenge_fingerprint",
        page=page,
    )
    return _ImageVerificationHandleResult(
        page=required.page,
        error=required.error,
        audit=audit,
    )


def _new_verification_challenge_page(
    handled: _ImageVerificationHandleResult,
) -> Any | None:
    error = handled.error or {}
    if (
        error.get("error_code") == "jisou_image_verification_required"
        and error.get("image_verification_reason") == "new_challenge_fingerprint"
    ):
        return handled.page
    return None


def _classify_jisou_navigation_page(
    page: Any,
    protocol_profile: object,
) -> tuple[list[SearchJoinButton], ProtocolPageClassification]:
    buttons = _parse_buttons(page)
    classification = _jisou_page_classification(
        True,
        protocol_profile,
        page,
        buttons,
    )
    return buttons, classification


def _jisou_navigation_phase_error(
    classification: ProtocolPageClassification,
    buttons: list[SearchJoinButton],
) -> dict[str, Any] | None:
    if classification.page_phase == "verification_page":
        return _protocol_phase_error(
            "bot_human_verification_required",
            "搜索机器人要求人机验证，当前账号不能自动执行",
            classification,
            buttons,
        )
    if classification.page_phase == "hot_list_page":
        return _protocol_phase_error(
            "jisou_hot_list_page",
            "极搜关键词响应为热搜排行榜页，当前尝试失败并排除账号协议路径 12 小时",
            classification,
            buttons,
        )
    if classification.page_phase in {"search_category_page", "group_result_page"}:
        return None
    return _protocol_phase_error(
        "jisou_session_state_deviated",
        "极搜关键词响应未匹配已知协议页面，账号会话状态偏离，排除 12 小时",
        classification,
        buttons,
    )


def _navigation_group_selector(
    classification: ProtocolPageClassification,
    buttons: list[SearchJoinButton],
) -> SearchJoinButton | None:
    if classification.page_phase == "search_category_page":
        return _find_button_by_position(
            buttons,
            classification.selector_positions,
        )
    if classification.page_phase != "hot_list_page":
        return None
    return next(
        (
            button
            for button in buttons
            if button.row == 0
            and button.col == 0
            and button.button_type == "callback_data"
            and _normalized_button_text(button.text)
            == _normalized_button_text("👥")
        ),
        None,
    )


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
    entity_links = search_result_entity_links(page)
    if is_group_result_entity_page(entity_links, buttons):
        return ProtocolPageClassification(
            "group_result_page",
            approved_navigation_positions(buttons),
            frozenset(),
        )
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
    audit: dict[str, Any] | None = None
    image_bytes: bytes = b""


async def _handle_jisou_image_verification(
    client: Any,
    bot_username: str,
    page: Any,
    buttons: list[SearchJoinButton],
    classification: ProtocolPageClassification,
    solver: ImageVerificationSolver | None,
    *,
    protocol_profile: object,
    callback_unknown_fingerprints: frozenset[str] = frozenset(),
) -> _ImageVerificationHandleResult:
    """Handle one immutable challenge fingerprint and one approved submit."""
    observed_at = datetime.now(UTC)
    observed_monotonic = monotonic()
    answer_buttons = [
        button for button in buttons
        if _is_verification_answer_button(button)
    ]
    candidate_answers = tuple(
        button.text.strip() for button in answer_buttons
    )
    image_bytes = await _download_verification_image(client, page)
    fingerprint = _image_verification_fingerprint(
        page,
        buttons,
        image_bytes,
    )
    if not image_bytes:
        return _image_verification_required_result(
            classification, buttons, fingerprint, "verification_transport_unavailable"
        )
    if fingerprint in callback_unknown_fingerprints:
        return _image_verification_callback_unknown_result(
            classification,
            buttons,
            fingerprint,
            bot_username=bot_username,
            detail="同一验证码此前 callback 已发送但结果未知，禁止重复识别和点击",
            page=page,
        )
    if solver is None or not candidate_answers:
        return _image_verification_required_result(
            classification, buttons, fingerprint, "verification_ai_unavailable"
        )
    mime_type = _message_media_mime_type(page)
    identity = _image_verification_identity(
        bot_username,
        page,
        candidate_answers,
        image_bytes,
    )
    request = ImageVerificationRequest(
        image_bytes=image_bytes,
        mime_type=mime_type,
        candidate_answers=candidate_answers,
        challenge_text=_message_text(page),
        challenge_fingerprint_hash=fingerprint,
        message_id=identity["message_id"],
        message_revision=identity["message_revision"],
        bot_peer_hash=identity["bot_peer_hash"],
        image_hash=identity["image_hash"],
        candidate_hash=identity["candidate_hash"],
        challenge_observed_at=observed_at,
        challenge_observed_monotonic=observed_monotonic,
    )
    try:
        solved = await _solve_with_unknown_recovery(
            solver,
            request,
            client,
            bot_username,
            page,
            fingerprint,
            protocol_profile,
        )
        if isinstance(solved, _ImageVerificationHandleResult):
            return solved
    except ImageVerificationRuntimeContractError as exc:
        return _image_verification_required_result(
            classification,
            buttons,
            fingerprint,
            exc.code,
            detail=str(exc),
            votes=exc.votes,
        )
    except ImageVerificationConsensusUnavailableError as exc:
        return _image_verification_required_result(
            classification,
            buttons,
            fingerprint,
            "verification_consensus_unavailable",
            detail=str(exc),
            votes=exc.votes,
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
        return _image_verification_required_result(
            classification,
            buttons,
            fingerprint,
            "verification_consensus_unavailable",
            detail="recognition returned no consensus decision",
        )
    answer = solved.answer
    confidence = solved.confidence
    audit = _image_verification_audit(solved, request)
    preflight = await _verification_callback_preflight(
        client,
        bot_username,
        page,
        fingerprint,
        solved.callback_submit_deadline_monotonic,
        protocol_profile=protocol_profile,
    )
    if preflight.error is not None:
        return preflight
    current_page = preflight.page
    current_buttons = _parse_buttons(current_page)
    target_button = next(
        (
            button for button in current_buttons
            if button.text.strip() == answer
            and _is_verification_answer_button(button)
        ),
        None,
    )
    if target_button is None:
        return _image_verification_failed_result(
            classification, buttons, f"answer {answer} not in button matrix",
            answer=answer, confidence=confidence, fingerprint=fingerprint,
        )
    try:
        clicked_page = await _click_verification_and_get_response(
            client,
            bot_username,
            current_page,
            target_button,
            deadline_monotonic=solved.callback_submit_deadline_monotonic,
        )
    except _VerificationCallbackDeadlineExceeded as exc:
        return _image_verification_required_result(
            classification,
            buttons,
            fingerprint,
            "verification_deadline_exceeded",
            detail=str(exc),
            page=current_page,
            votes=solved.votes,
        )
    except _VerificationCallbackResultUnknown as exc:
        return _image_verification_callback_unknown_result(
            classification,
            buttons,
            fingerprint,
            bot_username=bot_username,
            detail=str(exc),
            page=current_page,
            votes=solved.votes,
        )
    clicked_buttons = _parse_buttons(clicked_page)
    clicked_classification = classify_jisou_page_with_media(
        profile=protocol_profile,
        message_text=_message_text(clicked_page),
        buttons=clicked_buttons,
        has_photo=_has_photo_media(clicked_page),
    )
    if clicked_classification.page_phase == VERIFICATION_IMAGE_PAGE:
        next_image_bytes = await _download_verification_image(
            client,
            clicked_page,
        )
        next_fingerprint = _image_verification_fingerprint(
            clicked_page,
            clicked_buttons,
            next_image_bytes,
        )
        if not next_image_bytes:
            return _image_verification_required_result(
                clicked_classification,
                clicked_buttons,
                next_fingerprint,
                "verification_transport_unavailable",
                page=clicked_page,
            )
        if next_fingerprint == fingerprint:
            return _image_verification_failed_result(
                clicked_classification,
                clicked_buttons,
                "remote explicitly rejected approved answer",
                answer=answer,
                confidence=confidence,
                fingerprint=fingerprint,
                votes=solved.votes,
            )
        return _image_verification_required_result(
            clicked_classification,
            clicked_buttons,
            next_fingerprint,
            "new_challenge_fingerprint",
            page=clicked_page,
            votes=solved.votes,
        )
    return _ImageVerificationHandleResult(
        page=clicked_page,
        error=None,
        audit=audit,
    )


async def _solve_with_unknown_recovery(
    solver: ImageVerificationSolver,
    request: ImageVerificationRequest,
    client: Any,
    bot_username: str,
    page: Any,
    fingerprint: str,
    protocol_profile: object,
) -> ImageVerificationDecision | None | _ImageVerificationHandleResult:
    try:
        return await asyncio.to_thread(solver, request)
    except ImageVerificationRuntimeContractError as exc:
        if exc.code != "verification_local_ocr_unknown":
            raise
        refreshed = await _refresh_unknown_ocr_request(
            client,
            bot_username,
            page,
            fingerprint,
            exc.callback_submit_deadline_monotonic,
            request,
            protocol_profile,
        )
        if isinstance(refreshed, _ImageVerificationHandleResult):
            return refreshed
        return await asyncio.to_thread(solver, refreshed)


async def _refresh_unknown_ocr_request(
    client: Any,
    bot_username: str,
    page: Any,
    fingerprint: str,
    deadline_monotonic: float,
    request: ImageVerificationRequest,
    protocol_profile: object,
) -> ImageVerificationRequest | _ImageVerificationHandleResult:
    preflight = await _verification_callback_preflight(
        client,
        bot_username,
        page,
        fingerprint,
        deadline_monotonic,
        protocol_profile=protocol_profile,
    )
    if preflight.error is not None:
        return preflight
    current_page = preflight.page
    image_bytes = preflight.image_bytes or await _download_verification_image(
        client,
        current_page,
    )
    buttons = _parse_buttons(current_page)
    candidates = tuple(
        button.text.strip()
        for button in buttons
        if _is_verification_answer_button(button)
    )
    identity = _image_verification_identity(
        bot_username,
        current_page,
        candidates,
        image_bytes,
    )
    return replace(
        request,
        image_bytes=image_bytes,
        mime_type=_message_media_mime_type(current_page),
        candidate_answers=candidates,
        challenge_text=_message_text(current_page),
        message_id=identity["message_id"],
        message_revision=identity["message_revision"],
        bot_peer_hash=identity["bot_peer_hash"],
        image_hash=identity["image_hash"],
        candidate_hash=identity["candidate_hash"],
    )


def _image_verification_fingerprint(
    page: Any,
    buttons: list[SearchJoinButton],
    image_bytes: bytes,
) -> str:
    message_id = str(getattr(page, "id", "") or "")
    callback_fingerprint = [
        (button.row, button.col, button.text, button.button_type)
        for button in buttons
    ]
    image_hash = hashlib.sha256(image_bytes).hexdigest()
    value = repr(
        (message_id, image_hash, callback_fingerprint)
    ).encode()
    return hashlib.sha256(value).hexdigest()


def _image_verification_identity(
    bot_username: str,
    page: Any,
    candidate_answers: tuple[str, ...],
    image_bytes: bytes,
) -> dict[str, str]:
    return {
        "bot_peer_hash": hashlib.sha256(
            bot_username.strip().lower().encode()
        ).hexdigest(),
        "message_id": str(getattr(page, "id", "") or ""),
        "message_revision": _page_revision_fingerprint(page),
        "image_hash": hashlib.sha256(image_bytes).hexdigest(),
        "candidate_hash": hashlib.sha256(
            repr(candidate_answers).encode()
        ).hexdigest(),
    }


async def _verification_callback_preflight(
    client: Any,
    bot_username: str,
    page: Any,
    expected_fingerprint: str,
    deadline_monotonic: float,
    *,
    protocol_profile: object,
) -> _ImageVerificationHandleResult:
    classification = classify_jisou_page_with_media(
        profile=protocol_profile,
        message_text=_message_text(page),
        buttons=_parse_buttons(page),
        has_photo=_has_photo_media(page),
    )
    if not deadline_monotonic:
        return _ImageVerificationHandleResult(page=page, error=None)
    if deadline_monotonic and monotonic() >= deadline_monotonic:
        return _image_verification_required_result(
            classification,
            _parse_buttons(page),
            expected_fingerprint,
            "verification_deadline_exceeded",
        )
    current_page = await client.get_messages(bot_username, ids=page.id)
    current_buttons = _parse_buttons(current_page) if current_page else []
    current_image = (
        await _download_verification_image(client, current_page)
        if current_page else b""
    )
    current_fingerprint = _image_verification_fingerprint(
        current_page,
        current_buttons,
        current_image,
    ) if current_page and current_image else ""
    if current_fingerprint != expected_fingerprint:
        current_classification = classify_jisou_page_with_media(
            profile=protocol_profile,
            message_text=_message_text(current_page),
            buttons=current_buttons,
            has_photo=_has_photo_media(current_page),
        )
        return _image_verification_required_result(
            current_classification,
            current_buttons,
            current_fingerprint,
            "new_challenge_fingerprint",
            page=current_page,
        )
    if deadline_monotonic and monotonic() >= deadline_monotonic:
        return _image_verification_required_result(
            classification,
            current_buttons,
            expected_fingerprint,
            "verification_deadline_exceeded",
            page=current_page,
        )
    return _ImageVerificationHandleResult(
        page=current_page,
        error=None,
        image_bytes=current_image,
    )


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


def _is_verification_answer_button(button: SearchJoinButton) -> bool:
    if button.button_type != "callback_data":
        return False
    text = button.text.strip()
    return bool(text) and bool(
        VERIFICATION_ANSWER_PATTERN.fullmatch(text)
    )


def _image_verification_failed_result(
    classification: ProtocolPageClassification,
    buttons: list[SearchJoinButton],
    detail: str,
    *,
    answer: str = "",
    confidence: float = 0.0,
    fingerprint: str = "",
    votes: tuple[ImageVerificationVote, ...] = (),
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
        "image_verification_votes": [vote.as_dict() for vote in votes],
        "challenge_fingerprint_hash": fingerprint,
        "search_protocol_trace": {
            "page_phase": VERIFICATION_IMAGE_PAGE,
            "page": _page_layout(buttons, classification.approved_button_positions),
        },
    }
    return _ImageVerificationHandleResult(page=None, error=error)


def _image_verification_budget_exhausted(
    classification: ProtocolPageClassification,
    buttons: list[SearchJoinButton],
) -> _ImageVerificationHandleResult:
    return _image_verification_failed_result(
        classification,
        buttons,
        "image verification challenge budget exhausted",
    )


def _image_verification_required_result(
    classification: ProtocolPageClassification,
    buttons: list[SearchJoinButton],
    fingerprint: str,
    reason: str,
    *,
    detail: str = "",
    page: Any = None,
    votes: tuple[ImageVerificationVote, ...] = (),
) -> _ImageVerificationHandleResult:
    error = {
        **_failed("jisou_image_verification_required", "极搜图片验证码等待安全识别结果"),
        "jisou_page_phase": VERIFICATION_IMAGE_PAGE,
        "protocol_event_type": "image_verification_required",
        "image_verification_status": "required",
        "image_verification_reason": reason,
        "image_verification_detail": detail,
        "image_verification_votes": [vote.as_dict() for vote in votes],
        "challenge_fingerprint_hash": fingerprint,
        "search_protocol_trace": {
            "page_phase": VERIFICATION_IMAGE_PAGE,
            "page": _page_layout(buttons, classification.approved_button_positions),
        },
    }
    audit = None
    if votes:
        audit = {
            "status": reason,
            "challenge_fingerprint_hash": fingerprint,
            "votes": [vote.as_dict() for vote in votes],
        }
    return _ImageVerificationHandleResult(
        page=page,
        error=error,
        audit=audit,
    )


def _image_verification_callback_unknown_result(
    classification: ProtocolPageClassification,
    buttons: list[SearchJoinButton],
    fingerprint: str,
    *,
    bot_username: str,
    detail: str,
    page: Any = None,
    votes: tuple[ImageVerificationVote, ...] = (),
) -> _ImageVerificationHandleResult:
    error = {
        **_failed(
            "verification_callback_result_unknown",
            "验证码 callback 已发送，但结果未能确认",
        ),
        "jisou_page_phase": VERIFICATION_IMAGE_PAGE,
        "protocol_event_type": "image_verification_callback_unknown",
        "image_verification_status": "unknown",
        "image_verification_reason": "verification_callback_result_unknown",
        "image_verification_detail": detail,
        "image_verification_votes": [vote.as_dict() for vote in votes],
        "challenge_fingerprint_hash": fingerprint,
        "callback_mutation_started": True,
        "bot_username": bot_username.strip().lower().lstrip("@"),
        "search_protocol_trace": {
            "page_phase": VERIFICATION_IMAGE_PAGE,
            "page": _page_layout(buttons, classification.approved_button_positions),
        },
    }
    audit = {
        "status": "verification_callback_result_unknown",
        "challenge_fingerprint_hash": fingerprint,
        "votes": [vote.as_dict() for vote in votes],
    }
    return _ImageVerificationHandleResult(page=page, error=error, audit=audit)


def _image_verification_audit(
    decision: ImageVerificationDecision,
    request: ImageVerificationRequest,
) -> dict[str, Any]:
    return {
        "status": "consensus_submitted",
        "answer": decision.answer,
        "confidence": round(decision.confidence, 4),
        "model_waited": decision.model_waited,
        "model_started": decision.model_started,
        "model_start_reason": decision.model_start_reason,
        "consensus_source": decision.consensus_source,
        "contract_version": decision.contract_version,
        "challenge_observed_at": decision.challenge_observed_at,
        "model_hedge_at": decision.model_hedge_at,
        "callback_submit_deadline": decision.callback_submit_deadline,
        "challenge_fingerprint_hash": request.challenge_fingerprint_hash,
        "bot_peer_hash": request.bot_peer_hash,
        "message_id": request.message_id,
        "message_revision": request.message_revision,
        "image_hash": request.image_hash,
        "candidate_hash": request.candidate_hash,
        "votes": [vote.as_dict() for vote in decision.votes],
    }


def _with_image_verification_attempts(
    result: dict[str, Any],
    attempts: list[dict[str, Any]],
) -> dict[str, Any]:
    if not attempts:
        return result
    return {
        **result,
        "image_verification_attempts": attempts,
        "image_verification_challenge_count": len(
            {
                str(attempt.get("challenge_fingerprint_hash") or "")
                for attempt in attempts
                if attempt.get("challenge_fingerprint_hash")
            }
        ),
        "image_verification_refresh_count": sum(
            1
            for attempt in attempts
            if attempt.get("refresh_kind") == "keyword_replay"
        ),
    }


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


@dataclass(frozen=True)
class _TargetEntityOpenContext:
    client: Any
    page: Any
    payload: dict[str, Any]
    target: dict[str, Any]
    decoys: list[dict[str, Any]]
    page_no: int
    total: int


async def _execute_target_entity_open(
    context: _TargetEntityOpenContext,
    link: SearchResultEntityLink,
) -> dict[str, Any]:
    target_username = str(context.target.get("username") or "")
    opened = await open_target_entity(
        context.client,
        link,
        target_username,
    )
    result = {
        **_success(
            context.payload,
            None,
            context.total,
            context.decoys,
            context.page_no,
        ),
        "target_position": link.position,
        "target_match_source": "message_entity_text_url",
        "target_message_id": str(
            getattr(context.page, "id", "") or ""
        ),
        "target_username": target_username,
        "bot_username": _bot_username(context.payload),
    }
    if (
        context.payload.get("search_execution_mode")
        != "click_only"
    ):
        return result
    return {
        **result,
        "target_click_observed": True,
        "membership_side_effect": "none",
        "membership_mutating_rpc_invoked": False,
        "target_button_type": "message_entity_text_url",
        "target_button_effect": "target_open_only",
        "target_button_fingerprint": target_entity_fingerprint(
            link,
            opened,
        ),
        "target_entity_url_hash": _text_hash(link.url.lower()),
        "target_entity_id": opened.entity_id,
        "target_entity_username": opened.username,
        "target_entity_title_hash": _text_hash(opened.title),
        "target_entity_title_length": len(opened.title),
        "target_open_rpc": opened.rpc,
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
    original_fingerprint = _page_revision_fingerprint(message)
    await _click_button(message, button)
    return await _wait_for_callback_page(
        client,
        bot_username,
        message,
        original_fingerprint=original_fingerprint,
    )


async def _click_verification_and_get_response(
    client: Any,
    bot_username: str,
    message: Any,
    button: SearchJoinButton,
    *,
    deadline_monotonic: float,
) -> Any:
    original_fingerprint = _page_revision_fingerprint(message)
    if deadline_monotonic and monotonic() >= deadline_monotonic:
        raise _VerificationCallbackDeadlineExceeded(
            "callback submit deadline elapsed immediately before click"
        )
    try:
        await _click_button(message, button)
        return await _wait_for_callback_page(
            client,
            bot_username,
            message,
            original_fingerprint=original_fingerprint,
        )
    except Exception as exc:
        raise _VerificationCallbackResultUnknown(
            str(exc) or exc.__class__.__name__
        ) from exc


async def _wait_for_callback_page(
    client: Any,
    bot_username: str,
    message: Any,
    *,
    original_fingerprint: str,
) -> Any:
    loop = asyncio.get_running_loop()
    deadline = (
        loop.time() + CALLBACK_PAGE_RESPONSE_TIMEOUT_SECONDS
    )
    while loop.time() < deadline:
        edited_page = await client.get_messages(
            bot_username,
            ids=message.id,
        )
        if (
            edited_page is not None
            and _page_revision_fingerprint(edited_page)
            != original_fingerprint
        ):
            return edited_page
        newer_page = await _latest_newer_bot_message(
            client,
            bot_username,
            message,
        )
        if newer_page is not None:
            return newer_page
        await asyncio.sleep(CALLBACK_PAGE_POLL_INTERVAL_SECONDS)
    raise RuntimeError("callback_page_response_unavailable")


def _page_revision_fingerprint(message: Any) -> str:
    buttons = [
        (
            button.row,
            button.col,
            button.text,
            button.button_type,
            button.effect,
            button.url,
        )
        for button in _parse_buttons(message)
    ]
    entities = [
        (
            type(entity).__name__,
            str(getattr(entity, "url", "") or ""),
            int(getattr(entity, "offset", 0) or 0),
            int(getattr(entity, "length", 0) or 0),
        )
        for entity in getattr(message, "entities", None) or []
    ]
    media = getattr(message, "media", None)
    photo = getattr(media, "photo", None)
    value = (
        int(getattr(message, "id", 0) or 0),
        _message_text(message),
        buttons,
        entities,
        type(media).__name__,
        str(getattr(photo, "id", "") or ""),
    )
    return hashlib.sha256(repr(value).encode()).hexdigest()


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
    "ImageVerificationConsensusUnavailableError",
    "ImageVerificationDecision",
    "ImageVerificationNoSafeAnswerError",
    "ImageVerificationProviderUnavailableError",
    "ImageVerificationRequest",
    "ImageVerificationSolver",
    "ImageVerificationVote",
    "SearchJoinButton",
    "execute_search_join_with_client",
]

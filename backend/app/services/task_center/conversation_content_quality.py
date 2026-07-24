"""Shared humanized content quality gate and 签到 fallback."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any


CHECK_IN_TEXT = "签到"
TEMPLATE_SHELLS = (
    "这个点挺有意思",
    "可以继续聊聊",
    "值得讨论",
    "角度不错",
    "先收藏",
    "参考价值",
    "继续展开",
    "支持一下",
    "感谢分享",
    "有人在吗",
    "这事可以再看看",
    "我也来冒个泡",
)


@dataclass(frozen=True)
class QualityDecision:
    allowed: bool
    code: str = ""
    reason: str = ""


@dataclass(frozen=True)
class FallbackDecision:
    allowed: bool
    content: str = ""
    content_source: str = ""
    fallback_reason: str = ""
    code: str = ""


def evaluate_conversation_content(
    *,
    content: str,
    intent: str = "",
    history: list[str] | None = None,
    reply_to_message_id: int | str | None = None,
    context_anchors: list[str] | None = None,
    voice_keywords: list[str] | None = None,
) -> QualityDecision:
    text = str(content or "").strip()
    if not text:
        return QualityDecision(False, code="empty_content", reason="empty")
    if text == CHECK_IN_TEXT:
        return QualityDecision(True, code="check_in", reason="exact_check_in")

    lowered = text.lower()
    for shell in TEMPLATE_SHELLS:
        if shell in text:
            return QualityDecision(False, code="template_shell", reason=shell)

    history_items = [str(item or "").strip() for item in (history or []) if str(item or "").strip()]
    if history_items:
        opening = _opening_token(text)
        if opening and any(_opening_token(item) == opening for item in history_items[-8:]):
            return QualityDecision(False, code="repeated_opening", reason=opening)
        if any(_normalize(item) == _normalize(text) for item in history_items[-12:]):
            return QualityDecision(False, code="semantic_duplicate", reason="exact_or_normalized_duplicate")
        if any(_jaccard(_tokens(text), _tokens(item)) >= 0.82 for item in history_items[-8:]):
            return QualityDecision(False, code="semantic_duplicate", reason="high_token_overlap")

    anchors = [str(item or "").strip() for item in (context_anchors or []) if str(item or "").strip()]
    if intent not in {"check_in", "reaction"} and anchors:
        if not any(anchor[:8] in text or any(tok in text for tok in _tokens(anchor)[:3]) for anchor in anchors):
            # Allow short concrete questions without full anchor copy.
            if not (text.endswith("？") or text.endswith("?")):
                return QualityDecision(False, code="missing_context_anchor", reason="no_anchor_overlap")

    if reply_to_message_id and intent == "reply":
        # Reply actions must not invent unlinked praise-only shells.
        if len(text) >= 8 and not anchors:
            return QualityDecision(False, code="reply_target_mismatch", reason="reply_without_anchor")

    if voice_keywords:
        # Soft mismatch only when content is long and has none of the voice cues.
        if len(text) >= 24 and not any(str(k) in text for k in voice_keywords if str(k).strip()):
            return QualityDecision(False, code="voice_profile_mismatch", reason="no_voice_keyword")

    return QualityDecision(True, code="ok")


def resolve_content_fallback(
    *,
    is_reply: bool,
    static_fallback_enabled: bool,
    last_platform_content_source: str = "",
    last_platform_text: str = "",
    session_check_in_count_30m: int = 0,
    task_hour_check_in_count: int = 0,
    task_hour_check_in_limit: int = 3,
    fallback_reason: str = "quality_or_generation_failed",
) -> FallbackDecision:
    if is_reply:
        return FallbackDecision(False, code="reply_cannot_use_check_in")
    if not static_fallback_enabled:
        return FallbackDecision(False, code="static_fallback_disabled")
    if last_platform_content_source == "check_in_fallback" or str(last_platform_text or "").strip() == CHECK_IN_TEXT:
        return FallbackDecision(False, code="check_in_repeat")
    if int(session_check_in_count_30m or 0) >= 3:
        return FallbackDecision(False, code="check_in_quota_exceeded", fallback_reason="session_30m")
    if int(task_hour_check_in_count or 0) >= max(1, int(task_hour_check_in_limit or 3)):
        return FallbackDecision(False, code="check_in_quota_exceeded", fallback_reason="task_hour")
    return FallbackDecision(
        True,
        content=CHECK_IN_TEXT,
        content_source="check_in_fallback",
        fallback_reason=fallback_reason,
        code="check_in_fallback",
    )


def _opening_token(text: str) -> str:
    cleaned = re.sub(r"\s+", "", text or "")
    return cleaned[:4]


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", str(text or "")).lower()


def _tokens(text: str) -> set[str]:
    parts = re.findall(r"[\w\u4e00-\u9fff]+", str(text or "").lower())
    return {part for part in parts if part}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


__all__ = [
    "CHECK_IN_TEXT",
    "QualityDecision",
    "FallbackDecision",
    "evaluate_conversation_content",
    "resolve_content_fallback",
]

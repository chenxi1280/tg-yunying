from __future__ import annotations

import hashlib
from typing import Any

from .comment_fallback_selection import UNICODE_EMOJI_ALLOWLIST_V2


COMMENT_GENERATION_ATTEMPTS_PER_MODEL = 3
COMMENT_EMOJI_FALLBACKS = ("👍", "🙂", "👏")
STRUCTURAL_COMMENT_FAILURES = frozenset({
    "comment_unavailable_message",
    "peer_invalid",
    "reply_target_missing",
    "reply_target_stale",
    "rule_version_unavailable",
})


def comment_generation_stages() -> tuple[str, ...]:
    return (
        *("primary_m3" for _ in range(COMMENT_GENERATION_ATTEMPTS_PER_MODEL)),
        *("fallback_m25" for _ in range(COMMENT_GENERATION_ATTEMPTS_PER_MODEL)),
    )


def ordered_fallback_emojis(request: Any) -> tuple[str, ...]:
    key = f"{request.task_id}:{request.payload.channel_message_id}:{request.payload.slot_id}"
    offset = int(hashlib.sha256(key.encode("utf-8")).hexdigest()[:8], 16)
    config = getattr(request, "config", {})
    pool = (
        UNICODE_EMOJI_ALLOWLIST_V2
        if config.get("channel_comment_grounding_v1_enabled")
        or config.get("unicode_emoji_allowlist_v2")
        else COMMENT_EMOJI_FALLBACKS
    )
    index = offset % len(pool)
    return pool[index:] + pool[:index]


def grounding_contract(request: Any) -> bool:
    frozen_grounding = (
        getattr(request.payload, "grounding_assignment_id", "")
        or getattr(request.payload, "grounding_enrollment_id", "")
        or getattr(request.payload, "discussion_group_binding_id", "")
    )
    return bool(request.config.get("channel_comment_grounding_v1_enabled") or frozen_grounding)


def mask_fallback_attempt(reason: str) -> dict:
    return {
        "stage": "phase_a",
        "outcome": "deterministic_fallback",
        "reason": reason,
    }


def provider_failure(stage: str, exc: Exception) -> dict:
    return {
        "stage": stage,
        "outcome": "provider_failed",
        "reason": str(exc),
    }


def quality_attempt(stage: str, decision: Any) -> dict:
    return {
        "stage": stage,
        "outcome": "accepted" if decision.allowed else "rejected",
        "reason": decision.code,
    }


def fallback_reason(attempts: list[dict]) -> str:
    reasons = [
        str(item.get("reason") or item.get("outcome") or "")
        for item in attempts
    ]
    return ",".join(item for item in reasons if item) or "all_model_stages_rejected"


def reply_target(payload: Any) -> dict:
    return {
        "message_id": int(payload.reply_to_message_id or 0),
        "author": payload.reply_target_author,
        "preview": payload.reply_target_preview,
        "source": payload.reply_target_source,
    }

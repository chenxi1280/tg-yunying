from __future__ import annotations

import random

from ..pacing_quantity import deterministic_rank
from ..reaction_intent import (
    classify_emoji_intent,
    detect_negative_keywords,
    resolve_safe_reactions,
)


ACTIONABLE_CAPABILITY_MODES = frozenset({"all", "some"})


def reaction_plan(
    reactions: list[str],
    quantity: int,
    reaction_type: str = "random",
    *,
    seed_id: str = "",
    reaction_scope: str = "configured",
    available_reactions: list[str] | None = None,
    reaction_capability_mode: str = "unknown",
    content_text: str = "",
    safe_intents: set[str] | None = None,
) -> list[str]:
    if quantity <= 0:
        return []
    configured = _normalize_reactions(reactions)
    available = _normalize_reactions(available_reactions)
    if reaction_capability_mode not in ACTIONABLE_CAPABILITY_MODES:
        return []
    if reaction_type == "specific":
        return _specific_plan(configured, available, quantity, content_text=content_text)
    pool, _decision, _ = resolve_safe_reactions(
        configured if reaction_scope != "all_available" else [],
        available,
        content_text=content_text,
        safe_intents=safe_intents,
    )
    if not pool:
        return []
    rng = random.Random(deterministic_rank(seed_id, "reaction-plan"))
    return rng.choices(pool, k=quantity)


def _specific_plan(
    configured: list[str],
    available: list[str],
    quantity: int,
    *,
    content_text: str = "",
) -> list[str]:
    if not configured:
        return []
    target = configured[0]
    intent = classify_emoji_intent(target)
    if intent == "celebrate" and detect_negative_keywords(content_text):
        return []
    available_map = {_normalize_emoji_key(item): item for item in available}
    key = _normalize_emoji_key(target)
    if key not in available_map:
        return []
    return [available_map[key]] * quantity


def _normalize_reactions(reactions: list[str] | None) -> list[str]:
    normalized: list[str] = []
    for reaction in reactions or []:
        value = str(reaction).strip()
        if value and value not in normalized:
            normalized.append(value)
    return normalized


def _normalize_emoji_key(value: str) -> str:
    return value.replace("\ufe0f", "").replace("\ufe0e", "").strip()


def _intersection(configured: list[str], available: list[str]) -> list[str]:
    available_map = {_normalize_emoji_key(item): item for item in available}
    result: list[str] = []
    for reaction in configured:
        key = _normalize_emoji_key(reaction)
        if key in available_map:
            matched = available_map[key]
            if matched not in result:
                result.append(matched)
    return result


__all__ = ["reaction_plan"]


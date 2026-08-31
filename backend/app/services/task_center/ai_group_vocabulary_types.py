"""Shared immutable types and constructors for AI group vocabulary data."""

from __future__ import annotations

from dataclasses import dataclass

@dataclass(frozen=True)
class VocabularyUnit:
    vocabulary_id: str
    category: str
    surface_terms: tuple[str, ...]
    normalized_term_ids: tuple[str, ...]
    theme_tags: tuple[int, ...]
    theme_weights: dict[str, int]
    allowed_routes: tuple[str, ...]
    allowed_act_types: tuple[str, ...]
    allowed_stances: tuple[str, ...]
    fact_class: str
    rarity: str  # common | rare


def _u(
    vid: str,
    cat: str,
    surfaces: list[str],
    norms: list[str],
    tags: list[int],
    weights: dict[str, int],
    routes: list[str],
    acts: list[str],
    stances: list[str],
    fact: str = "context_bound",
    rarity: str = "common",
) -> VocabularyUnit:
    return VocabularyUnit(
        vocabulary_id=vid,
        category=cat,
        surface_terms=tuple(surfaces),
        normalized_term_ids=tuple(norms),
        theme_tags=tuple(tags),
        theme_weights={str(theme): int(weights.get(str(theme), 1)) for theme in tags},
        allowed_routes=tuple(routes),
        allowed_act_types=tuple(acts),
        allowed_stances=tuple(stances),
        fact_class=fact,
        rarity=rarity,
    )


ALL_ADULT_ROUTES = [
    "adult_service",
    "adult_visual",
    "adult_product",
    "adult_service_inquiry",
    "adult_service_sensory",
]
ALL_GENERAL_ROUTES = ["general_chat", "general_lifestyle", "general_qa"]
ALL_ACTS = [
    "context_reply",
    "short_react",
    "question",
    "detail_follow",
    "light_disagree",
    "topic_shift",
]
ALL_STANCES = ["neutral", "positive", "negative", "reserved"]

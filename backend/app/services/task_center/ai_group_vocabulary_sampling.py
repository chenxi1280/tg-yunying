"""Deterministic slot-level vocabulary sampling and cooldown guards."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import hashlib
import re

from .ai_group_vocabulary_catalog import (
    ADULT_COMPATIBILITY_MANIFEST,
    GENERAL_COMPATIBILITY_MANIFEST,
    VocabularyUnit,
    get_vocabulary_catalog,
)


VOCABULARY_CATALOG_VERSION = "v1.2.0"
MAX_SAMPLES_PER_SLOT = 2
MIN_RUNTIME_EFFECTIVE_POOL = 4
RARE_COOLDOWN_WINDOW = 20
COMMON_COOLDOWN_WINDOW = 10
MAX_TERM_RATIO_100_WINDOW = 0.05
MAX_2GRAM_REPEAT_20_WINDOW = 2
STOP_SURFACE_BIGRAMS = frozenset(
    {"这个", "那个", "就是", "还是", "可以", "感觉", "有点", "一下", "怎么", "什么"}
)


@dataclass(frozen=True)
class VocabularySamplingResult:
    sample_units: tuple[VocabularyUnit, ...]
    sample_ids: tuple[str, ...]
    effective_state: (
        str  # active | pool_exhausted | effective_pool_low | not_applicable
    )
    candidate_count: int


def _filter_candidates(
    catalog: list[VocabularyUnit],
    *,
    daily_theme_id: int,
    route: str,
    act_type: str | None,
    stance: str | None,
    topic_mode: str,
    evidence_text: str,
    is_generic_warmup: bool,
    recent_id_messages: list[tuple[str, ...]],
    term_counts: dict[str, int],
) -> list[VocabularyUnit]:
    candidates: list[VocabularyUnit] = []
    for u in catalog:
        if daily_theme_id not in u.theme_tags or route not in u.allowed_routes:
            continue
        if act_type and u.allowed_act_types and act_type not in u.allowed_act_types:
            continue
        if stance and u.allowed_stances and stance not in u.allowed_stances:
            continue
        if not _fact_class_compatible(
            u,
            topic_mode=topic_mode,
            evidence_text=evidence_text,
            is_generic_warmup=is_generic_warmup,
        ):
            continue
        cooldown = (
            RARE_COOLDOWN_WINDOW if u.rarity == "rare" else COMMON_COOLDOWN_WINDOW
        )
        if any(
            u.vocabulary_id in message_ids
            for message_ids in recent_id_messages[:cooldown]
        ):
            continue
        if any(term_counts.get(t, 0) >= 5 for t in u.normalized_term_ids):
            continue
        candidates.append(u)
    return candidates


def _fact_class_compatible(
    unit: VocabularyUnit,
    *,
    topic_mode: str,
    evidence_text: str,
    is_generic_warmup: bool,
) -> bool:
    if unit.fact_class == "expression_only":
        return True
    if is_generic_warmup or topic_mode == "group_free_chat":
        return False
    normalized_evidence = re.sub(
        r"[^\w\u4e00-\u9fff]+", "", str(evidence_text or "").lower()
    )
    if not normalized_evidence:
        return False
    anchors = (*unit.normalized_term_ids, *unit.surface_terms)
    return any(
        re.sub(r"[^\w\u4e00-\u9fff]+", "", anchor.lower()) in normalized_evidence
        for anchor in anchors
        if anchor
    )


def _rank_and_select(
    candidates: list[VocabularyUnit],
    seed_str: str,
    daily_theme_id: int,
) -> tuple[VocabularyUnit, ...]:
    def _rank_key(unit: VocabularyUnit) -> tuple[int, int]:
        w = unit.theme_weights.get(str(daily_theme_id), 1)
        digest = hashlib.sha256(
            (seed_str + ":" + unit.vocabulary_id).encode("utf-8")
        ).digest()
        u_hash = int.from_bytes(digest[:4], "big")
        return (-w, u_hash)

    sorted_candidates = sorted(candidates, key=_rank_key)
    sample_count = min(MAX_SAMPLES_PER_SLOT, len(sorted_candidates))
    return tuple(sorted_candidates[:sample_count])


def sample_vocabulary_for_slot(
    *,
    surface_scope_key: str,
    task_day: date,
    allocation_plan_id: str,
    plan_unit_ordinal: int,
    daily_vocabulary_theme_id: int,
    route: str = "adult_service",
    route_family: str = "adult",
    act_type: str | None = None,
    stance: str | None = None,
    topic_mode: str = "group_free_chat",
    evidence_text: str = "",
    is_generic_warmup: bool = False,
    recent_used_ids_by_message: list[tuple[str, ...]] | None = None,
    recent_term_counts: dict[str, int] | None = None,
) -> VocabularySamplingResult:
    if not act_type or not stance:
        return VocabularySamplingResult((), (), "effective_pool_low", 0)
    candidates = _runtime_candidates(
        route_family=route_family,
        daily_theme_id=daily_vocabulary_theme_id,
        route=route,
        act_type=act_type,
        stance=stance,
        topic_mode=topic_mode,
        evidence_text=evidence_text,
        is_generic_warmup=is_generic_warmup,
        recent_id_messages=list(recent_used_ids_by_message or []),
        term_counts=recent_term_counts or {},
    )
    if not candidates:
        return VocabularySamplingResult((), (), "pool_exhausted", 0)
    if len(candidates) < MIN_RUNTIME_EFFECTIVE_POOL:
        return VocabularySamplingResult((), (), "effective_pool_low", len(candidates))

    seed_str = f"{surface_scope_key}:{task_day.isoformat()}:{allocation_plan_id}:{plan_unit_ordinal}:{daily_vocabulary_theme_id}:{VOCABULARY_CATALOG_VERSION}"
    selected = _rank_and_select(candidates, seed_str, daily_vocabulary_theme_id)
    state = "active"
    return VocabularySamplingResult(
        selected, tuple(u.vocabulary_id for u in selected), state, len(candidates)
    )


def _runtime_candidates(
    *,
    route_family: str,
    daily_theme_id: int,
    route: str,
    act_type: str,
    stance: str,
    topic_mode: str,
    evidence_text: str,
    is_generic_warmup: bool,
    recent_id_messages: list[tuple[str, ...]],
    term_counts: dict[str, int],
) -> list[VocabularyUnit]:
    candidates = _filter_candidates(
        get_vocabulary_catalog(route_family),
        daily_theme_id=daily_theme_id,
        route=route,
        act_type=act_type,
        stance=stance,
        topic_mode=topic_mode,
        evidence_text=evidence_text,
        is_generic_warmup=is_generic_warmup,
        recent_id_messages=recent_id_messages,
        term_counts=term_counts,
    )
    manifest = (
        GENERAL_COMPATIBILITY_MANIFEST
        if route_family == "general"
        else ADULT_COMPATIBILITY_MANIFEST
    )
    return _manifest_candidates(
        candidates,
        manifest,
        daily_theme_id=daily_theme_id,
        route=route,
        act_type=act_type,
        stance=stance,
    )


def _manifest_candidates(
    candidates: list[VocabularyUnit],
    manifest,
    *,
    daily_theme_id: int,
    route: str,
    act_type: str,
    stance: str,
) -> list[VocabularyUnit]:
    return [
        unit
        for unit in candidates
        if (daily_theme_id, route, act_type, stance, unit.fact_class) in manifest
    ]


def extract_vocabulary_usage(
    content: str,
    route_family: str = "adult",
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    catalog = get_vocabulary_catalog(route_family)
    used_ids: list[str] = []
    used_terms: list[str] = []
    for u in catalog:
        if any(surf in content for surf in u.surface_terms) or any(
            norm in content for norm in u.normalized_term_ids
        ):
            used_ids.append(u.vocabulary_id)
            used_terms.extend(u.normalized_term_ids)
    return tuple(used_ids), tuple(sorted(set(used_terms)))


def protected_frequency_phrases(
    topic_direction: dict | None,
    teacher_target: dict | None,
) -> tuple[str, ...]:
    phrases: set[str] = set()
    for snapshot in (topic_direction or {}, teacher_target or {}):
        _collect_protected_phrases(snapshot, phrases)
    return tuple(sorted(phrases, key=lambda value: (-len(value), value)))


def _collect_protected_phrases(value, phrases: set[str]) -> None:
    if isinstance(value, dict):
        for nested in value.values():
            _collect_protected_phrases(nested, phrases)
        return
    if isinstance(value, (list, tuple, set)):
        for nested in value:
            _collect_protected_phrases(nested, phrases)
        return
    if not isinstance(value, str):
        return
    normalized = re.sub(r"[^\w\u4e00-\u9fff]+", "", value.lower())
    if len(normalized) >= 2:
        phrases.add(normalized)


def surface_phrase_fingerprints(
    content: str,
    *,
    excluded_phrases: tuple[str, ...] = (),
) -> tuple[str, ...]:
    normalized = re.sub(r"[^\w\u4e00-\u9fff]+", "", str(content or "").lower())
    for phrase in excluded_phrases:
        excluded = re.sub(r"[^\w\u4e00-\u9fff]+", "", str(phrase).lower())
        if excluded:
            normalized = normalized.replace(excluded, " ")
    grams = {
        segment[index : index + 2]
        for segment in normalized.split()
        for index in range(max(0, len(segment) - 1))
        if segment[index : index + 2] not in STOP_SURFACE_BIGRAMS
    }
    return tuple(
        sorted(hashlib.sha256(gram.encode("utf-8")).hexdigest()[:16] for gram in grams)
    )


__all__ = [
    "VocabularySamplingResult",
    "extract_vocabulary_usage",
    "protected_frequency_phrases",
    "sample_vocabulary_for_slot",
    "surface_phrase_fingerprints",
]

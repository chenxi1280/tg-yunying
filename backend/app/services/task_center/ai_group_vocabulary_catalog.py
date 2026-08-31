"""Vocabulary catalog assembly and validation for AI group daily themes."""

from __future__ import annotations

from collections import Counter

from .ai_group_vocabulary_adult_a import _cat_app, _cat_att
from .ai_group_vocabulary_adult_b import _cat_coop, _cat_env
from .ai_group_vocabulary_adult_c import _cat_caut, _cat_sch
from .ai_group_vocabulary_adult_d import _cat_mini, _cat_stmt
from .ai_group_vocabulary_adult_e import _cat_pers, _cat_trans
from .ai_group_vocabulary_expression_data import EXPRESSION_PHRASES
from .ai_group_vocabulary_general_data import _cat_gen_group1, _cat_gen_group2
from .ai_group_vocabulary_types import (
    ALL_ACTS,
    ALL_ADULT_ROUTES,
    ALL_GENERAL_ROUTES,
    ALL_STANCES,
    VocabularyUnit,
    _u,
)

def _build_adult_catalog() -> list[VocabularyUnit]:
    items: list[VocabularyUnit] = []
    items.extend(_cat_app())
    items.extend(_cat_att())
    items.extend(_cat_coop())
    items.extend(_cat_env())
    items.extend(_cat_sch())
    items.extend(_cat_caut())
    items.extend(_cat_stmt())
    items.extend(_cat_mini())
    items.extend(_cat_trans())
    items.extend(_cat_pers())
    items.extend(_build_expression_catalog("adult", ALL_ADULT_ROUTES))
    return items


def _build_general_catalog() -> list[VocabularyUnit]:
    items: list[VocabularyUnit] = []
    cats = _cat_gen_group1() + _cat_gen_group2()
    idx = 1
    for cat_name, phrases in cats:
        for p in phrases:
            vid = f"gen_{cat_name[:4]}_{idx:03d}"
            idx += 1
            items.append(
                _u(
                    vid,
                    cat_name,
                    [p],
                    [p[:4]],
                    [0, 1, 2, 3, 4, 5, 6],
                    {"0": 1, "1": 1, "2": 1, "3": 1, "4": 1, "5": 1, "6": 1},
                    ALL_GENERAL_ROUTES,
                    ALL_ACTS,
                    ALL_STANCES,
                    rarity="common" if idx % 4 != 0 else "rare",
                )
            )
    items.extend(_build_expression_catalog("general", ALL_GENERAL_ROUTES))
    return items


def _build_expression_catalog(
    prefix: str,
    routes: list[str],
) -> list[VocabularyUnit]:
    units: list[VocabularyUnit] = []
    for (act_type, stance), phrases in EXPRESSION_PHRASES.items():
        category = f"expression_{act_type}"
        for index, phrase in enumerate(phrases, 1):
            weights = {
                str(theme): 1 + ((index + theme) % 3) for theme in range(7)
            }
            units.append(
                _u(
                    f"{prefix}_expr_{act_type}_{index:02d}",
                    category,
                    [phrase],
                    [phrase],
                    list(range(7)),
                    weights,
                    routes,
                    [act_type],
                    [stance],
                    fact="expression_only",
                    rarity="rare" if index % 4 == 0 else "common",
                )
            )
    return units


ADULT_VOCABULARY_CATALOG: list[VocabularyUnit] = _build_adult_catalog()
GENERAL_VOCABULARY_CATALOG: list[VocabularyUnit] = _build_general_catalog()


MIN_PUBLISHED_CELL_SIZE = 12
ADULT_PUBLISHED_CELL_PATTERNS = {
    ("context_reply", "positive", "context_bound"): (0, 1, 3, 5, 6),
    ("detail_follow", "neutral", "context_bound"): (1, 2, 4, 6),
    ("detail_follow", "positive", "context_bound"): (1, 3, 5, 6),
    ("detail_follow", "reserved", "context_bound"): (1, 2, 4),
    ("light_disagree", "reserved", "context_bound"): (4,),
    ("question", "neutral", "context_bound"): (2, 6),
    ("short_react", "neutral", "context_bound"): (0, 6),
    ("short_react", "positive", "context_bound"): (0, 1, 5, 6),
}
EXPRESSION_PUBLISHED_CELL_PATTERNS = {
    (act_type, stance, "expression_only"): tuple(range(7))
    for act_type, stance in EXPRESSION_PHRASES
}


def _adult_compatibility_manifest() -> frozenset[tuple[int, str, str, str, str]]:
    return frozenset(
        (theme, route, act_type, stance, fact_class)
        for (
            act_type,
            stance,
            fact_class,
        ), themes in {
            **ADULT_PUBLISHED_CELL_PATTERNS,
            **EXPRESSION_PUBLISHED_CELL_PATTERNS,
        }.items()
        for theme in themes
        for route in ALL_ADULT_ROUTES
    )


def _general_compatibility_manifest() -> frozenset[tuple[int, str, str, str, str]]:
    context_cells = frozenset(
        (theme, route, act_type, stance, "context_bound")
        for theme in range(7)
        for route in ALL_GENERAL_ROUTES
        for act_type in ALL_ACTS
        for stance in ALL_STANCES
    )
    expression_cells = frozenset(
        (theme, route, act_type, stance, fact_class)
        for (act_type, stance, fact_class), themes in EXPRESSION_PUBLISHED_CELL_PATTERNS.items()
        for theme in themes
        for route in ALL_GENERAL_ROUTES
    )
    return context_cells | expression_cells


ADULT_COMPATIBILITY_MANIFEST = _adult_compatibility_manifest()
GENERAL_COMPATIBILITY_MANIFEST = _general_compatibility_manifest()


def _compatibility_cell_counts(
    catalog: list[VocabularyUnit],
) -> Counter[tuple[int, str, str, str, str]]:
    cells: Counter[tuple[int, str, str, str, str]] = Counter()
    for unit in catalog:
        for theme in unit.theme_tags:
            for route in unit.allowed_routes:
                for act_type in unit.allowed_act_types:
                    for stance in unit.allowed_stances:
                        cells[(theme, route, act_type, stance, unit.fact_class)] += 1
    return cells


def get_vocabulary_catalog(route_family: str = "adult") -> list[VocabularyUnit]:
    if route_family == "general":
        return GENERAL_VOCABULARY_CATALOG
    return ADULT_VOCABULARY_CATALOG


def validate_vocabulary_catalog(
    catalog: list[VocabularyUnit], min_units: int = 120
) -> tuple[bool, str]:
    if len(catalog) < min_units:
        return False, f"catalog_size_insufficient: {len(catalog)} < {min_units}"
    ids = set()
    categories: Counter[str] = Counter()
    for u in catalog:
        if u.vocabulary_id in ids:
            return False, f"duplicate_vocabulary_id: {u.vocabulary_id}"
        ids.add(u.vocabulary_id)
        categories[u.category] += 1
        if not u.theme_tags:
            return False, f"empty_theme_tags: {u.vocabulary_id}"
        if not u.surface_terms:
            return False, f"empty_surface_terms: {u.vocabulary_id}"
        if not u.normalized_term_ids:
            return False, f"empty_normalized_term_ids: {u.vocabulary_id}"
        if not u.allowed_routes:
            return False, f"empty_allowed_routes: {u.vocabulary_id}"
        if not u.allowed_act_types:
            return False, f"empty_allowed_act_types: {u.vocabulary_id}"
        if not u.allowed_stances:
            return False, f"empty_allowed_stances: {u.vocabulary_id}"
        if u.fact_class not in {"expression_only", "context_bound"}:
            return False, f"invalid_fact_class: {u.vocabulary_id}"
        if u.rarity not in {"common", "rare"}:
            return False, f"invalid_rarity: {u.vocabulary_id}"
        if any(str(theme) not in u.theme_weights for theme in u.theme_tags):
            return False, f"missing_theme_weight: {u.vocabulary_id}"
    undersized = sorted(
        category for category, count in categories.items() if count < 12
    )
    if undersized:
        return False, f"category_size_insufficient: {','.join(undersized)}"
    manifest = _manifest_for_catalog(catalog)
    cell_counts = _compatibility_cell_counts(catalog)
    missing = sorted(
        cell for cell in manifest if cell_counts[cell] < MIN_PUBLISHED_CELL_SIZE
    )
    if missing:
        return False, f"published_cell_size_insufficient: {missing[0]}"
    return True, "ok"


def _manifest_for_catalog(
    catalog: list[VocabularyUnit],
) -> frozenset[tuple[int, str, str, str, str]]:
    routes = {route for unit in catalog for route in unit.allowed_routes}
    if routes and routes <= set(ALL_GENERAL_ROUTES):
        return GENERAL_COMPATIBILITY_MANIFEST
    return ADULT_COMPATIBILITY_MANIFEST


__all__ = [
    "ADULT_VOCABULARY_CATALOG",
    "ADULT_COMPATIBILITY_MANIFEST",
    "GENERAL_VOCABULARY_CATALOG",
    "GENERAL_COMPATIBILITY_MANIFEST",
    "VocabularyUnit",
    "get_vocabulary_catalog",
    "validate_vocabulary_catalog",
]

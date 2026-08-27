from __future__ import annotations

import random

from ..pacing_quantity import deterministic_rank


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
) -> list[str]:
    if quantity <= 0:
        return []
    configured = _normalize_reactions(reactions)
    available = _normalize_reactions(available_reactions)
    if reaction_capability_mode not in ACTIONABLE_CAPABILITY_MODES:
        return []
    if reaction_type == "specific":
        return _specific_plan(configured, available, quantity)
    pool = available if reaction_scope == "all_available" else _intersection(configured, available)
    if not pool:
        return []
    rng = random.Random(deterministic_rank(seed_id, "reaction-plan"))
    return rng.choices(pool, k=quantity)


def _specific_plan(
    configured: list[str],
    available: list[str],
    quantity: int,
) -> list[str]:
    if not configured or configured[0] not in available:
        return []
    return [configured[0]] * quantity


def _normalize_reactions(reactions: list[str] | None) -> list[str]:
    normalized: list[str] = []
    for reaction in reactions or []:
        value = str(reaction).strip()
        if value and value not in normalized:
            normalized.append(value)
    return normalized


def _intersection(configured: list[str], available: list[str]) -> list[str]:
    available_set = set(available)
    return [reaction for reaction in configured if reaction in available_set]


__all__ = ["reaction_plan"]

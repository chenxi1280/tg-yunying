from __future__ import annotations

import random

from ..pacing_quantity import deterministic_rank


PRIMARY_REACTION_RATIO = 0.7
EXTRA_REACTION_RATIO = 0.1
MIN_EXTRA_REACTION_QUANTITY = 10
DEFAULT_EXTRA_REACTIONS = ("👏", "🎉", "😁", "🤩", "👌", "🙏", "💯", "⚡")


def reaction_plan(
    reactions: list[str],
    quantity: int,
    reaction_type: str = "random",
    *,
    seed_id: str = "",
) -> list[str]:
    normalized = _normalize_reactions(reactions)
    if quantity <= 0:
        return []
    if reaction_type == "specific" or len(normalized) == 1:
        return [normalized[0]] * quantity
    primary_count = _primary_reaction_count(quantity)
    extra_count = _extra_reaction_count(normalized, quantity, primary_count)
    secondary_count = max(0, quantity - primary_count - extra_count)
    plan = [normalized[0]] * primary_count
    rng = random.Random(deterministic_rank(seed_id, "reaction-plan"))
    plan.extend(_secondary_reactions(normalized[1:], secondary_count, rng=rng))
    plan.extend(_extra_reactions(normalized, extra_count, rng=rng))
    rng.shuffle(plan)
    return plan


def _normalize_reactions(reactions: list[str]) -> list[str]:
    normalized: list[str] = []
    for reaction in reactions or []:
        value = str(reaction).strip()
        if value and value not in normalized:
            normalized.append(value)
    return normalized or ["👍"]


def _primary_reaction_count(quantity: int) -> int:
    if quantity <= 1:
        return quantity
    return min(quantity, max(1, round(quantity * PRIMARY_REACTION_RATIO)))


def _extra_reaction_count(
    reactions: list[str],
    quantity: int,
    primary_count: int,
) -> int:
    extra_pool = [reaction for reaction in DEFAULT_EXTRA_REACTIONS if reaction not in reactions]
    if quantity < MIN_EXTRA_REACTION_QUANTITY or not extra_pool:
        return 0
    secondary_minimum = min(len(reactions) - 1, max(0, quantity - primary_count))
    extra_room = max(0, quantity - primary_count - secondary_minimum)
    return min(extra_room, len(extra_pool), max(1, round(quantity * EXTRA_REACTION_RATIO)))


def _secondary_reactions(
    reactions: list[str],
    quantity: int,
    *,
    rng: random.Random,
) -> list[str]:
    if quantity <= 0 or not reactions:
        return []
    guaranteed = list(reactions[: min(quantity, len(reactions))])
    remaining = quantity - len(guaranteed)
    selected = guaranteed + rng.choices(reactions, k=remaining)
    rng.shuffle(selected)
    return selected


def _extra_reactions(
    configured_reactions: list[str],
    quantity: int,
    *,
    rng: random.Random,
) -> list[str]:
    extra_pool = [
        reaction
        for reaction in DEFAULT_EXTRA_REACTIONS
        if reaction not in configured_reactions
    ]
    return rng.sample(extra_pool, k=min(quantity, len(extra_pool)))


__all__ = ["reaction_plan"]

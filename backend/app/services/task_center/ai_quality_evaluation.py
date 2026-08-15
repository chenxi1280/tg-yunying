from __future__ import annotations

import random
from collections import defaultdict
from dataclasses import dataclass


DEFAULT_BOOTSTRAP_ITERATIONS = 2000


@dataclass(frozen=True)
class PositionSwapResult:
    winner: str
    position_consistent: bool
    confidence: float
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class PreferenceSample:
    context_cluster: str
    outcome: str


@dataclass(frozen=True)
class PreferenceInterval:
    point_estimate: float | None
    lower_95: float | None
    upper_95: float | None
    effective_count: int
    tie_count: int
    cluster_count: int


def aggregate_position_swap(
    forward: dict,
    reverse: dict,
) -> PositionSwapResult:
    first = _mapped_winner(forward, reverse_order=False)
    second = _mapped_winner(reverse, reverse_order=True)
    evidence = tuple(
        str(item)
        for payload in (forward, reverse)
        for item in (payload.get("evidence") or [])
        if str(item)
    )
    if first == second and first in {"candidate", "baseline"}:
        confidence = min(_confidence(forward), _confidence(reverse))
        return PositionSwapResult(first, True, confidence, evidence)
    return PositionSwapResult("tie", first == second == "tie", 0.0, evidence)


def cluster_bootstrap_preference_interval(
    samples: list[PreferenceSample],
    *,
    seed: str,
    iterations: int = DEFAULT_BOOTSTRAP_ITERATIONS,
) -> PreferenceInterval:
    clusters: dict[str, list[PreferenceSample]] = defaultdict(list)
    for sample in samples:
        if sample.outcome not in {"candidate", "baseline", "tie"}:
            raise ValueError("preference_sample_outcome_invalid")
        clusters[sample.context_cluster].append(sample)
    effective = [sample for sample in samples if sample.outcome != "tie"]
    ties = len(samples) - len(effective)
    if not clusters or not effective:
        return PreferenceInterval(None, None, None, 0, ties, len(clusters))
    rng = random.Random(seed)
    keys = sorted(clusters)
    estimates = [
        _resampled_preference(rng, keys, clusters)
        for _ in range(max(1, iterations))
    ]
    valid = sorted(value for value in estimates if value is not None)
    point = _preference_rate(effective)
    return PreferenceInterval(
        point,
        _quantile(valid, 0.025),
        _quantile(valid, 0.975),
        len(effective),
        ties,
        len(clusters),
    )


def _mapped_winner(payload: dict, *, reverse_order: bool) -> str:
    winner = str(payload.get("winner") or "tie").upper()
    if winner == "TIE":
        return "tie"
    if winner not in {"A", "B"}:
        raise ValueError("pairwise_winner_invalid")
    if reverse_order:
        return "baseline" if winner == "A" else "candidate"
    return "candidate" if winner == "A" else "baseline"


def _confidence(payload: dict) -> float:
    value = float(payload.get("confidence") or 0.0)
    if not 0 <= value <= 1:
        raise ValueError("pairwise_confidence_invalid")
    return value


def _resampled_preference(rng, keys, clusters) -> float | None:
    selected: list[PreferenceSample] = []
    for _ in keys:
        selected.extend(clusters[rng.choice(keys)])
    effective = [sample for sample in selected if sample.outcome != "tie"]
    return _preference_rate(effective) if effective else None


def _preference_rate(samples: list[PreferenceSample]) -> float:
    wins = sum(sample.outcome == "candidate" for sample in samples)
    return wins / len(samples)


def _quantile(values: list[float], ratio: float) -> float | None:
    if not values:
        return None
    index = round((len(values) - 1) * ratio)
    return values[index]


__all__ = [
    "PreferenceInterval",
    "PreferenceSample",
    "PositionSwapResult",
    "aggregate_position_swap",
    "cluster_bootstrap_preference_interval",
]

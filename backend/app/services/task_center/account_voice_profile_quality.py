from __future__ import annotations

from collections.abc import Callable
from difflib import SequenceMatcher
from json import JSONDecodeError
from typing import Any

SUMMARY_SIMILARITY_BLOCK_THRESHOLD = 0.82
BATCH_DIVERSITY_MAX_ATTEMPTS = 3
RECOVERABLE_GENERATION_ERRORS = (ValueError, RuntimeError, TimeoutError, JSONDecodeError)


def generate_diverse_voice_profile_batch(
    generator: Callable[[list[int]], list[dict[str, Any]]],
    account_ids: list[int],
) -> tuple[dict[int, dict[str, Any]], dict[int, int]]:
    last_error: Exception | None = None
    for _attempt in range(BATCH_DIVERSITY_MAX_ATTEMPTS):
        try:
            profiles = _profiles_by_account(generator(account_ids))
            _validate_complete_batch(profiles, account_ids)
            scores = validate_batch_voice_profile_diversity(profiles, account_ids)
            return profiles, scores
        except RECOVERABLE_GENERATION_ERRORS as exc:
            last_error = exc
    raise last_error or ValueError("voice profile batch generation failed")


def validate_batch_voice_profile_diversity(profiles: dict[int, dict[str, Any]], account_ids: list[int]) -> dict[int, int]:
    if len(account_ids) < 2:
        return {account_id: 100 for account_id in account_ids}
    scores = _validate_summary_similarity(profiles, account_ids)
    _validate_style_signature_diversity(profiles, account_ids)
    return scores


def _validate_summary_similarity(profiles: dict[int, dict[str, Any]], account_ids: list[int]) -> dict[int, int]:
    summaries = {account_id: _normalized_summary(profiles[account_id]) for account_id in account_ids}
    max_similarity = {account_id: 0.0 for account_id in account_ids}
    for index, account_id in enumerate(account_ids):
        for other_id in account_ids[index + 1:]:
            similarity = _summary_similarity(summaries[account_id], summaries[other_id])
            max_similarity[account_id] = max(max_similarity[account_id], similarity)
            max_similarity[other_id] = max(max_similarity[other_id], similarity)
            if similarity >= SUMMARY_SIMILARITY_BLOCK_THRESHOLD:
                raise ValueError(f"voice profiles too similar for accounts {account_id},{other_id}")
    return {account_id: max(0, round((1 - max_similarity[account_id]) * 100)) for account_id in account_ids}


def _validate_style_signature_diversity(profiles: dict[int, dict[str, Any]], account_ids: list[int]) -> None:
    signatures = [_style_signature(profiles[account_id]) for account_id in account_ids]
    if any(not any(signature) for signature in signatures):
        return
    if len(set(signatures)) == 1:
        raise ValueError("voice profiles too similar: style signatures are identical")


def _validate_complete_batch(profiles: dict[int, dict[str, Any]], account_ids: list[int]) -> None:
    for account_id in account_ids:
        if account_id not in profiles:
            raise ValueError(f"voice profile missing for account {account_id}")


def _profiles_by_account(generated: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    return {int(item["account_id"]): item for item in generated if item.get("account_id") is not None}


def _summary_similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0
    if left == right:
        return 1
    return SequenceMatcher(None, left, right).ratio()


def _normalized_summary(profile: dict[str, Any]) -> str:
    return "".join(str(profile.get("short_prompt_summary") or "").lower().split())


def _style_signature(profile: dict[str, Any]) -> tuple[str, str, str, str, str, str]:
    return (
        _normalized_scalar(profile.get("mask_name")),
        _normalized_scalar(profile.get("identity_frame")),
        _normalized_scalar(profile.get("age_band")),
        _normalized_scalar(profile.get("sentence_length")),
        _normalized_scalar(profile.get("tone_strength")),
        _normalized_scalar(profile.get("emoji_policy")),
    )


def _normalized_scalar(value: Any) -> str:
    return str(value or "").strip().lower()


__all__ = ["generate_diverse_voice_profile_batch", "validate_batch_voice_profile_diversity"]

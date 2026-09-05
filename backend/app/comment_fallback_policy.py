from __future__ import annotations

from collections.abc import Mapping
from typing import Any


FALLBACK_WEIGHT_TOTAL = 10000
DEFAULT_PLANNED_FALLBACK_MAX_BPS = 2000
DISABLED_FALLBACK_ZERO_FIELDS = (
    "planned_fallback_max_bps", "unicode_emoji_weight_bps", "image_meme_weight_bps",
)


def validate_comment_fallback_policy(config: Mapping[str, Any]) -> bool:
    """Validate the frozen policy shape and return whether a fallback is enabled."""
    unicode_enabled = bool(config.get("unicode_emoji_enabled", True))
    image_enabled = bool(config.get("image_meme_enabled", False))
    if not unicode_enabled and not image_enabled:
        if all(config.get(field) == 0 for field in DISABLED_FALLBACK_ZERO_FIELDS):
            return False
        raise ValueError("comment_fallback_type_required")
    _validate_enabled_policy_weights(config)
    return True


def _validate_enabled_policy_weights(config: Mapping[str, Any]) -> None:
    unicode_weight = int(config.get("unicode_emoji_weight_bps", FALLBACK_WEIGHT_TOTAL) or 0)
    image_weight = int(config.get("image_meme_weight_bps", 0) or 0)
    if unicode_weight + image_weight != FALLBACK_WEIGHT_TOTAL:
        raise ValueError("comment_fallback_weights_must_total_10000")
    if not config.get("unicode_emoji_enabled", True) and unicode_weight:
        raise ValueError("unicode_emoji_weight_requires_enabled_type")
    if not config.get("image_meme_enabled", False) and image_weight:
        raise ValueError("image_meme_weight_requires_enabled_type")
    if image_weight > 0 and not config.get("image_meme_material_group_id"):
        raise ValueError("image_meme_material_group_required")

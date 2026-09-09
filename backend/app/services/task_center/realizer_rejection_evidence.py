from __future__ import annotations

import hashlib

from .message_brief import (
    MessageBrief,
    length_band_of,
    normalize_text,
    punctuation_profile_of,
)


SHAPE_REJECTION_CODES = frozenset({
    "realizer_length_band_mismatch",
    "realizer_punctuation_profile_mismatch",
})


def realizer_rejection_evidence(code: str, item: object, brief: MessageBrief) -> dict:
    """Expose shape diagnostics only after the parser has validated the object."""
    if code not in SHAPE_REJECTION_CODES:
        return {}
    content = normalize_text(item.get("content"))
    return {"realizer_shape": {
        "normalized_character_count": len(content),
        "expected_length_band": brief.length_band,
        "actual_length_band": length_band_of(content),
        "expected_punctuation_profile": brief.punctuation_profile,
        "actual_punctuation_profile": punctuation_profile_of(content),
        "candidate_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
    }}

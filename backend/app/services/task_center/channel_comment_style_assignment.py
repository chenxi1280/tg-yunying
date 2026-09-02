from __future__ import annotations

import hashlib
from dataclasses import dataclass


LENGTH_TIER_PATTERN = (
    "ultra_short",
    "ultra_short",
    "medium",
    "medium",
    "medium",
    "medium",
    "medium",
    "medium",
    "long",
    "long",
)
LENGTH_RANGES = {
    "ultra_short": (2, 6),
    "medium": (7, 16),
    "long": (18, 35),
}
PERSONA_KEYS = (
    "lurker",
    "witty",
    "veteran",
    "tempted",
    "casual",
)


@dataclass(frozen=True)
class CommentStyleAssignment:
    length_tier: str
    minimum_length: int
    maximum_length: int
    persona_key: str


def frozen_comment_style(
    grounding_snapshot_id: str,
    target_ordinal: int,
) -> CommentStyleAssignment:
    identity = f"{grounding_snapshot_id}:{int(target_ordinal)}"
    digest = hashlib.sha256(identity.encode("utf-8")).digest()
    offset = int.from_bytes(
        hashlib.sha256(grounding_snapshot_id.encode("utf-8")).digest()[:4], "big",
    ) % len(LENGTH_TIER_PATTERN)
    tier = LENGTH_TIER_PATTERN[
        (int(target_ordinal) - 1 + offset) % len(LENGTH_TIER_PATTERN)
    ]
    minimum, maximum = LENGTH_RANGES[tier]
    persona = PERSONA_KEYS[int.from_bytes(digest[:4], "big") % len(PERSONA_KEYS)]
    return CommentStyleAssignment(tier, minimum, maximum, persona)


def length_matches_style(content: str, assignment: CommentStyleAssignment) -> bool:
    length = len("".join(str(content or "").split()))
    return assignment.minimum_length <= length <= assignment.maximum_length


__all__ = [
    "CommentStyleAssignment",
    "LENGTH_TIER_PATTERN",
    "frozen_comment_style",
    "length_matches_style",
]

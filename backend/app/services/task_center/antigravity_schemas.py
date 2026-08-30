from __future__ import annotations

from typing import Any


BRIEF_PURPOSES = frozenset({"group_context_route", "comment_context_route"})
REALIZE_PURPOSES = frozenset({
    "group_realize_general",
    "group_realize_adult_visual",
    "group_realize_adult_product",
    "group_realize_adult_service_inquiry",
    "group_realize_adult_service_sensory",
    "comment_realize_general",
})


def antigravity_schema_for_purpose(purpose: str) -> dict[str, Any]:
    if purpose in BRIEF_PURPOSES:
        return _brief_schema()
    if purpose in REALIZE_PURPOSES:
        return _realizer_schema()
    return {"type": "object", "minProperties": 1}


def _brief_schema() -> dict[str, Any]:
    item = {
        "type": "object",
        "properties": {
            "slot_id": {"type": "string"},
            "speech_act": {"type": "string"},
            "stance": {"type": "string"},
            "length_band": {"type": "string"},
            "punctuation_profile": {"type": "string"},
            "anchor_ids": {"type": "array", "items": {"type": "string"}},
        },
        "required": [
            "slot_id", "speech_act", "stance", "length_band",
            "punctuation_profile", "anchor_ids",
        ],
        "additionalProperties": True,
    }
    return {
        "type": "object",
        "properties": {"briefs": {"type": "array", "items": item}},
        "required": ["briefs"],
        "additionalProperties": False,
    }


def _realizer_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "content": {"type": "string"},
            "used_anchor_ids": {"type": "array", "items": {"type": "string"}},
            "speech_act": {"type": "string"},
            "voice_profile_version": {"type": "string"},
        },
        "required": [
            "content", "used_anchor_ids", "speech_act", "voice_profile_version",
        ],
        "additionalProperties": True,
    }


__all__ = ["antigravity_schema_for_purpose"]

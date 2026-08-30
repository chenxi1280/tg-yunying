from __future__ import annotations

from typing import Any

from .message_brief import LENGTH_BANDS, SPEECH_ACTS, STANCES
from .message_brief_v2 import ADULT_MODES, CLAIM_SPEECH_ACTS, MODE_CLAIMS


BRIEF_PURPOSES = frozenset({"group_context_route", "comment_context_route"})
REALIZE_PURPOSES = frozenset({
    "group_realize_general",
    "group_realize_adult_visual",
    "group_realize_adult_product",
    "group_realize_adult_service_inquiry",
    "group_realize_adult_service_sensory",
    "comment_realize_general",
})


def antigravity_schema_for_purpose(
    purpose: str,
    config: dict | None = None,
) -> dict[str, Any]:
    if purpose in BRIEF_PURPOSES:
        return _brief_schema((config or {}).get("_ai_provider_planner_slots"))
    if purpose in REALIZE_PURPOSES:
        return _realizer_schema((config or {}).get("_ai_provider_realizer_contract"))
    return {"type": "object", "minProperties": 1}


def _brief_schema(raw_slots: object) -> dict[str, Any]:
    slots = [item for item in (raw_slots or ()) if isinstance(item, dict)]
    variants = [
        variant
        for slot in slots or [_generic_slot()]
        for variant in _brief_slot_variants(slot)
    ]
    array: dict[str, Any] = {"type": "array", "items": {"oneOf": variants}}
    if slots:
        array.update({"minItems": len(slots), "maxItems": len(slots)})
    return {
        "type": "object",
        "properties": {"briefs": array},
        "required": ["briefs"],
        "additionalProperties": False,
    }


def _brief_slot_variants(slot: dict) -> list[dict[str, Any]]:
    mode = str(slot.get("content_mode") or "general")
    categories = sorted(MODE_CLAIMS.get(mode, ()))
    if not categories:
        categories = sorted({
            category for values in MODE_CLAIMS.values() for category in values
        })
    return [_brief_variant(slot, mode, category) for category in categories]


def _brief_variant(slot: dict, mode: str, category: str) -> dict[str, Any]:
    speech_act = CLAIM_SPEECH_ACTS[category]
    evidence = [str(item) for item in (slot.get("route_evidence_ids") or ())]
    length_bands = ("micro", "short") if mode in ADULT_MODES else LENGTH_BANDS
    punctuation = ("question",) if speech_act == "question" else ("none", "pause")
    return {
        "type": "object",
        "properties": {
            "slot_id": _exact_or_string(str(slot.get("slot_id") or "")),
            "speech_act": {"type": "string", "enum": [speech_act]},
            "stance": {"type": "string", "enum": list(STANCES)},
            "length_band": {"type": "string", "enum": list(length_bands)},
            "punctuation_profile": {"type": "string", "enum": list(punctuation)},
            "anchor_ids": _evidence_array(evidence),
            "reply_to_message_id": _exact_or_string(
                str(slot.get("reply_to_message_id") or ""), allow_empty=True,
            ),
            "claims": _claim_array(category, speech_act, evidence),
        },
        "required": [
            "slot_id", "speech_act", "stance", "length_band",
            "punctuation_profile", "anchor_ids", "reply_to_message_id", "claims",
        ],
        "additionalProperties": False,
    }


def _claim_array(category: str, speech_act: str, evidence: list[str]) -> dict[str, Any]:
    return {
        "type": "array", "minItems": 1, "maxItems": 1,
        "items": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "enum": [category]},
                "speech_act": {"type": "string", "enum": [speech_act]},
                "evidence_ids": _evidence_array(evidence),
            },
            "required": ["category", "speech_act", "evidence_ids"],
            "additionalProperties": False,
        },
    }


def _realizer_schema(raw_contract: object) -> dict[str, Any]:
    contract = raw_contract if isinstance(raw_contract, dict) else {}
    anchor_ids = [str(item) for item in (contract.get("anchor_ids") or ())]
    speech_act = str(contract.get("speech_act") or "")
    voice_version = str(contract.get("voice_profile_version") or "")
    return {
        "type": "object",
        "properties": {
            "content": {"type": "string", "minLength": 1},
            "used_anchor_ids": _evidence_array(anchor_ids, require=bool(anchor_ids)),
            "speech_act": (
                {"type": "string", "enum": [speech_act]}
                if speech_act else {"type": "string", "enum": list(SPEECH_ACTS)}
            ),
            "voice_profile_version": _exact_or_string(voice_version),
        },
        "required": [
            "content", "used_anchor_ids", "speech_act", "voice_profile_version",
        ],
        "additionalProperties": False,
    }


def _evidence_array(values: list[str], *, require: bool = True) -> dict[str, Any]:
    item: dict[str, Any] = {"type": "string", "minLength": 1}
    if values:
        item["enum"] = values
    result: dict[str, Any] = {
        "type": "array", "uniqueItems": True, "items": item,
    }
    if require:
        result["minItems"] = 1
    return result


def _exact_or_string(value: str, *, allow_empty: bool = False) -> dict[str, Any]:
    if value or allow_empty:
        return {"type": "string", "enum": [value]}
    return {"type": "string", "minLength": 1}


def _generic_slot() -> dict[str, Any]:
    return {
        "slot_id": "", "reply_to_message_id": "",
        "content_mode": "", "route_evidence_ids": [],
    }


__all__ = ["antigravity_schema_for_purpose"]

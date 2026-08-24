from __future__ import annotations

from dataclasses import dataclass

from .ai_content_policy import VALID_ROUTES
from .ai_provider_routes import (
    GROUP_REVIEW_PURPOSE,
    GROUP_ROUTE_PURPOSE,
    REALIZE_PURPOSE_BY_MODE,
)


MANIFEST_ID = "ai_group_v2_canary_policy_v2"
BRIEF_CONTRACT_VERSION = "message_brief_v2"
VOICE_CONTRACT_VERSION = "voice_contract_v3"
EXAMPLE_SET_VERSION = "adult_human_anchors_v1"
MAX_GENERATION_LATENCY_SECONDS = 90
MAX_STYLE_OVERLAY_CHARS = 200
MAX_CONTENT_REGENERATIONS = 1
MAX_PROVIDER_CALLS_PER_SLOT = 6
NEGATIVE_LEXICON_VERSION = "generic_filler_v1"
NEGATIVE_PHRASES = (
    "签到",
    "打卡",
    "积分",
    "努力加油",
    "搬砖",
    "今天状态不错",
    "大家心情好",
)

PROMPT_VERSIONS = {
    "general": "general_v3",
    "adult_visual": "adult_visual_v1",
    "adult_product": "adult_product_v1",
    "adult_service_inquiry": "adult_service_inquiry_v1",
    "adult_service_sensory": "adult_service_sensory_v2",
}


@dataclass(frozen=True)
class BootstrapBudget:
    max_cost_per_slot: float
    daily_ai_budget: float


def policy_payload(budget: BootstrapBudget) -> dict:
    return {
        "manifest_id": MANIFEST_ID,
        "route_rules": {
            "allowed_routes": sorted(VALID_ROUTES),
            "brief_contract_version": BRIEF_CONTRACT_VERSION,
            "voice_contract_version": VOICE_CONTRACT_VERSION,
        },
        "prompt_registry": {
            route: {"version": version} for route, version in PROMPT_VERSIONS.items()
        },
        "gate_config": {
            "semantic_reviewer_required": True,
            "quality_wait_required": True,
            "legacy_static_fallback_enabled": False,
            "max_content_regenerations": MAX_CONTENT_REGENERATIONS,
            "max_provider_calls_per_slot": MAX_PROVIDER_CALLS_PER_SLOT,
            "max_generation_latency_seconds": MAX_GENERATION_LATENCY_SECONDS,
            "max_style_overlay_chars": MAX_STYLE_OVERLAY_CHARS,
            "max_cost_per_slot": budget.max_cost_per_slot,
            "daily_ai_budget": budget.daily_ai_budget,
            "forbidden_generation_sources": [
                "stage_1",
                "emoji_fallback",
                "static_fallback",
                "due_catch_up_check_in",
            ],
            "negative_lexicon": {
                "version": NEGATIVE_LEXICON_VERSION,
                "entries": [
                    {
                        "phrase": phrase,
                        "scope": "output",
                        "routes": ["*"],
                        "match_type": "contains",
                        "enabled": True,
                    }
                    for phrase in NEGATIVE_PHRASES
                ],
            },
        },
        "example_set": {"version": EXAMPLE_SET_VERSION},
    }


def required_purposes(allowed_routes: tuple[str, ...]) -> tuple[str, ...]:
    realizers = tuple(REALIZE_PURPOSE_BY_MODE[route] for route in allowed_routes)
    return (GROUP_ROUTE_PURPOSE, *dict.fromkeys(realizers), GROUP_REVIEW_PURPOSE)


__all__ = [
    "BootstrapBudget",
    "MANIFEST_ID",
    "policy_payload",
    "required_purposes",
]

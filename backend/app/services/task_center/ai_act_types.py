from __future__ import annotations

PRD_AI_GROUP_ACT_TYPES = frozenset(
    {
        "context_reply",
        "short_react",
        "question",
        "detail_follow",
        "light_disagree",
        "topic_shift",
        "emoji_react",
        "silence",
    }
)
LEGACY_AI_GROUP_ACT_TYPE_FALLBACK = "detail_follow"

_LEGACY_ACT_TYPE_ALIASES = {
    "light_question": "question",
    "side_comment": "light_disagree",
    "experience": "detail_follow",
    "追问": "question",
    "提问": "question",
    "问细节": "question",
    "观望": "light_disagree",
    "保留": "light_disagree",
}


def canonical_ai_group_act_type(act_type: str) -> str:
    value = str(act_type or "").strip()
    if not value:
        return ""
    if value in PRD_AI_GROUP_ACT_TYPES:
        return value
    return _LEGACY_ACT_TYPE_ALIASES.get(value, LEGACY_AI_GROUP_ACT_TYPE_FALLBACK)

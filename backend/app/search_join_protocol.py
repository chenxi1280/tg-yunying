from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable


JISOU_BOT_USERNAMES = frozenset({"jisou"})
PAGE_PHASE_ORDER = (
    "verification_page",
    "hot_list_page",
    "search_category_page",
    "group_result_page",
)
REQUIRED_PAGE_PHASES = frozenset(PAGE_PHASE_ORDER)
CONTROLLED_TEXT_ENUMS = {
    "human_verification": ("人机验证", "计算结果", "captcha"),
    "hot_list": ("热搜", "热门", "排行榜", "榜单", "hot list", "trending"),
    "jisou_group_category": ("👥", "群组", "群聊", "groups", "group", "👥群组", "👥群聊"),
    "jisou_channel_category": ("📢", "频道", "频道列表", "channels", "channel"),
}
ALLOWED_BUTTON_TYPES = frozenset({"callback_data", "telegram_url", "external_http_url", "unknown"})
ALLOWED_BUTTON_EFFECTS = frozenset({"unknown", "navigate_only", "target_open_only", "join_candidate", "external"})
ALLOWED_MEMBERSHIP_SIDE_EFFECTS = frozenset(
    {"none", "join", "request_to_join", "follow", "unknown"}
)
LEGACY_JISOU_CLICK_PROFILE_VERSION = "jisou-v2-2026-07-28"
PURE_CLICK_JISOU_PROFILE_VERSION = "jisou-click-only-v3-2026-07-29"

# PRD §2.19.1: verification_image_page 是独立相位，需图片、人机验证文本和至少 8 个 ASCII 答案按钮。
VERIFICATION_IMAGE_PAGE = "verification_image_page"
VERIFICATION_IMAGE_MIN_ANSWER_BUTTONS = 8
VERIFICATION_IMAGE_TEXT_MARKERS = ("人机验证", "计算结果", "captcha")
_ANSWER_BUTTON_TEXT_PATTERN = re.compile(r"^[A-Za-z0-9]+$")


@dataclass(frozen=True)
class ProtocolPageClassification:
    page_phase: str
    approved_button_positions: frozenset[int]
    selector_positions: frozenset[int]


def is_jisou_bot(bot_username: str) -> bool:
    return bot_username.strip().lower().lstrip("@") in JISOU_BOT_USERNAMES


def approved_protocol_profile(structure_json: object) -> dict[str, Any] | None:
    source = structure_json if isinstance(structure_json, dict) else {}
    fingerprints = source.get("page_fingerprints")
    if not isinstance(fingerprints, list):
        return None
    sanitized = [_sanitize_fingerprint(item) for item in fingerprints]
    if any(item is None for item in sanitized):
        return None
    valid = [item for item in sanitized if item is not None]
    if {item["page_phase"] for item in valid} != REQUIRED_PAGE_PHASES:
        return None
    return {"page_fingerprints": valid}


def protocol_profile_is_approved(profile: object) -> bool:
    return approved_protocol_profile(profile) is not None


def pure_click_protocol_profile_is_approved(profile: object) -> bool:
    approved = approved_protocol_profile(profile)
    if approved is None:
        return False
    result_pages = [
        item
        for item in approved["page_fingerprints"]
        if item["page_phase"] == "group_result_page"
    ]
    return bool(result_pages) and all(
        set(item["membership_side_effects_allowed"]) == {"none"}
        and "target_open_only" in set(item["button_effects_any"])
        and set(item["button_effects_any"]).issubset(
            {"navigate_only", "target_open_only"}
        )
        for item in result_pages
    )


def upgraded_legacy_pure_click_profile(
    profile: object,
    *,
    schema_version: str,
) -> dict[str, Any] | None:
    if schema_version != LEGACY_JISOU_CLICK_PROFILE_VERSION:
        return None
    approved = approved_protocol_profile(profile)
    if approved is None:
        return None
    result_pages = [
        item
        for item in approved["page_fingerprints"]
        if item["page_phase"] == "group_result_page"
    ]
    if len(result_pages) != 1:
        return None
    result_page = result_pages[0]
    effects = set(result_page["button_effects_any"])
    if result_page["membership_side_effects_allowed"]:
        return None
    if "join_candidate" not in effects or not effects.issubset(
        {"join_candidate", "navigate_only"}
    ):
        return None
    result_page["button_effects_any"] = [
        "target_open_only" if effect == "join_candidate" else effect
        for effect in result_page["button_effects_any"]
    ]
    result_page["membership_side_effects_allowed"] = ["none"]
    return approved


def classify_jisou_page(
    *,
    profile: object,
    message_text: str,
    buttons: Iterable[Any],
) -> ProtocolPageClassification:
    sanitized = approved_protocol_profile(profile)
    button_list = list(buttons)
    if sanitized is None:
        return _unknown_page()
    for phase in PAGE_PHASE_ORDER:
        for fingerprint in sanitized["page_fingerprints"]:
            if fingerprint["page_phase"] != phase:
                continue
            classification = _match_fingerprint(fingerprint, message_text, button_list)
            if classification is not None:
                return classification
    return _unknown_page()


def classify_jisou_page_with_media(
    *,
    profile: object,
    message_text: str,
    buttons: Iterable[Any],
    has_photo: bool = False,
) -> ProtocolPageClassification:
    """PRD §2.19.1: 先检测 verification_image_page（需图片、人机验证文本和至少 8 个 ASCII 答案按钮），
    命中则走图片验证码识别分支；否则回落到基于协议指纹的 classify_jisou_page。"""
    button_list = list(buttons)
    if has_photo and _is_verification_image_page(message_text, button_list):
        answer_positions = frozenset(
            _button_position(button)
            for button in button_list
            if _is_verification_answer_button(button)
        )
        return ProtocolPageClassification(
            page_phase=VERIFICATION_IMAGE_PAGE,
            approved_button_positions=answer_positions,
            selector_positions=frozenset(),
        )
    return classify_jisou_page(profile=profile, message_text=message_text, buttons=button_list)


def _is_verification_image_page(message_text: str, buttons: list[Any]) -> bool:
    normalized = normalize_visible_text(message_text)
    if not any(normalize_visible_text(marker) in normalized for marker in VERIFICATION_IMAGE_TEXT_MARKERS):
        return False
    answer_count = sum(
        1 for button in buttons
        if _is_verification_answer_button(button)
    )
    return answer_count >= VERIFICATION_IMAGE_MIN_ANSWER_BUTTONS


def _is_verification_answer_button(button: Any) -> bool:
    if _button_value(button, "button_type") != "callback_data":
        return False
    text = str(_button_value(button, "text") or "").strip()
    return bool(text) and bool(_ANSWER_BUTTON_TEXT_PATTERN.fullmatch(text))


def normalize_visible_text(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(character for character in text if not character.isspace())


def _sanitize_fingerprint(value: object) -> dict[str, Any] | None:
    source = value if isinstance(value, dict) else {}
    phase = str(source.get("page_phase") or "")
    if phase not in REQUIRED_PAGE_PHASES:
        return None
    text_enums = _enum_list(source, "text_enums", CONTROLLED_TEXT_ENUMS)
    button_text_enums = _enum_list(source, "button_text_enums_any", CONTROLLED_TEXT_ENUMS)
    button_effects = _enum_list(source, "button_effects_any", ALLOWED_BUTTON_EFFECTS)
    membership_effects = _enum_list(
        source,
        "membership_side_effects_allowed",
        ALLOWED_MEMBERSHIP_SIDE_EFFECTS,
    )
    required_buttons = _button_rules(source.get("required_buttons"), require_position=False)
    selector_rules = _selector_rules(source.get("selector_rules"))
    if None in (
        text_enums,
        button_text_enums,
        button_effects,
        membership_effects,
        required_buttons,
        selector_rules,
    ):
        return None
    if phase == "search_category_page" and not selector_rules:
        return None
    if phase != "search_category_page" and selector_rules:
        return None
    if not any((text_enums, button_text_enums, button_effects, required_buttons)):
        return None
    return {
        "page_phase": phase,
        "text_enums": text_enums,
        "button_text_enums_any": button_text_enums,
        "button_effects_any": button_effects,
        "membership_side_effects_allowed": membership_effects,
        "required_buttons": required_buttons,
        "selector_rules": selector_rules,
    }


def _enum_list(source: dict[str, Any], key: str, allowed: object) -> list[str] | None:
    if key not in source:
        return []
    values = source.get(key)
    allowed_values = set(allowed)
    if not isinstance(values, list):
        return None
    if not values:
        return []
    normalized = [str(value) for value in values]
    if any(value not in allowed_values for value in normalized):
        return None
    return list(dict.fromkeys(normalized))


def _button_rules(value: object, *, require_position: bool) -> list[dict[str, Any]] | None:
    if value is None:
        return []
    if not isinstance(value, list):
        return None
    if not value:
        return []
    rules = [_button_rule(item, require_position=require_position) for item in value]
    return None if any(rule is None for rule in rules) else [rule for rule in rules if rule is not None]


def _selector_rules(value: object) -> list[dict[str, Any]] | None:
    rules = _button_rules(value, require_position=True)
    if rules is None:
        return None
    if any(rule["button_type"] != "callback_data" or rule["effect"] != "unknown" for rule in rules):
        return None
    if any(rule.get("normalized_text") != "jisou_group_category" for rule in rules):
        return None
    return rules


def _button_rule(value: object, *, require_position: bool) -> dict[str, Any] | None:
    source = value if isinstance(value, dict) else {}
    button_type = str(source.get("button_type") or "")
    effect = str(source.get("effect") or "")
    normalized_text = str(source.get("normalized_text") or "")
    if button_type and button_type not in ALLOWED_BUTTON_TYPES:
        return None
    if effect and effect not in ALLOWED_BUTTON_EFFECTS:
        return None
    if normalized_text and normalized_text not in CONTROLLED_TEXT_ENUMS:
        return None
    if not any((button_type, effect, normalized_text)):
        return None
    row = _position(source, "row", required=require_position)
    col = _position(source, "col", required=require_position)
    if row is None or col is None:
        return None
    return {
        "row": row,
        "col": col,
        "button_type": button_type,
        "effect": effect,
        "normalized_text": normalized_text,
    }


def _position(source: dict[str, Any], key: str, *, required: bool) -> int | None:
    if key not in source:
        return -1 if not required else None
    value = source.get(key)
    return value if isinstance(value, int) and value >= 0 else None


def _match_fingerprint(
    fingerprint: dict[str, Any],
    message_text: str,
    buttons: list[Any],
) -> ProtocolPageClassification | None:
    if not _message_matches_enums(message_text, fingerprint["text_enums"]):
        return None
    matched = _matching_button_text_positions(buttons, fingerprint["button_text_enums_any"])
    if fingerprint["button_text_enums_any"] and not matched:
        return None
    effect_positions = _matching_effect_positions(buttons, fingerprint["button_effects_any"])
    if fingerprint["button_effects_any"] and not effect_positions:
        return None
    required_positions = _required_button_positions(buttons, fingerprint["required_buttons"])
    if required_positions is None:
        return None
    selector_positions = _selector_positions(buttons, fingerprint["selector_rules"])
    return ProtocolPageClassification(
        page_phase=fingerprint["page_phase"],
        approved_button_positions=frozenset(matched | effect_positions | required_positions | selector_positions),
        selector_positions=frozenset(selector_positions),
    )


def _message_matches_enums(message_text: str, enums: list[str]) -> bool:
    return all(_matches_controlled_message_text(message_text, enum) for enum in enums)


def _matches_controlled_message_text(message_text: str, enum: str) -> bool:
    normalized = normalize_visible_text(message_text)
    return any(normalize_visible_text(marker) in normalized for marker in CONTROLLED_TEXT_ENUMS[enum])


def _matching_button_text_positions(buttons: list[Any], enums: list[str]) -> set[int]:
    if not enums:
        return set()
    return {
        _button_position(button)
        for button in buttons
        if any(_matches_controlled_button_text(_button_value(button, "text"), enum) for enum in enums)
    }


def _matches_controlled_button_text(text: object, enum: str) -> bool:
    normalized = normalize_visible_text(text)
    return normalized in {normalize_visible_text(marker) for marker in CONTROLLED_TEXT_ENUMS[enum]}


def _matching_effect_positions(buttons: list[Any], effects: list[str]) -> set[int]:
    if not effects:
        return set()
    compatible_effects = set(effects)
    if "join_candidate" in compatible_effects:
        compatible_effects.add("target_open_only")
    return {
        _button_position(button)
        for button in buttons
        if _button_value(button, "effect") in compatible_effects
    }


def _required_button_positions(buttons: list[Any], rules: list[dict[str, Any]]) -> set[int] | None:
    positions: set[int] = set()
    for rule in rules:
        matches = {_button_position(button) for button in buttons if _button_matches_rule(button, rule)}
        if not matches:
            return None
        positions.update(matches)
    return positions


def _selector_positions(buttons: list[Any], rules: list[dict[str, Any]]) -> set[int]:
    return {
        _button_position(button)
        for rule in rules
        for button in buttons
        if _button_matches_rule(button, rule)
    }


def _button_matches_rule(button: Any, rule: dict[str, Any]) -> bool:
    if rule["row"] >= 0 and _button_value(button, "row") != rule["row"]:
        return False
    if rule["col"] >= 0 and _button_value(button, "col") != rule["col"]:
        return False
    if rule["button_type"] and _button_value(button, "button_type") != rule["button_type"]:
        return False
    if rule["effect"] and _button_value(button, "effect") != rule["effect"]:
        return False
    return not rule["normalized_text"] or _matches_controlled_button_text(_button_value(button, "text"), rule["normalized_text"])


def _button_value(button: Any, key: str) -> Any:
    return getattr(button, key, "")


def _button_position(button: Any) -> int:
    return int(_button_value(button, "position") or 0)


def _unknown_page() -> ProtocolPageClassification:
    return ProtocolPageClassification("unknown_page", frozenset(), frozenset())


__all__ = [
    "ProtocolPageClassification",
    "VERIFICATION_IMAGE_PAGE",
    "VERIFICATION_IMAGE_MIN_ANSWER_BUTTONS",
    "approved_protocol_profile",
    "classify_jisou_page",
    "classify_jisou_page_with_media",
    "is_jisou_bot",
    "normalize_visible_text",
    "pure_click_protocol_profile_is_approved",
    "PURE_CLICK_JISOU_PROFILE_VERSION",
    "protocol_profile_is_approved",
    "upgraded_legacy_pure_click_profile",
]

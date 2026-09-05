"""Deterministic output contract for explicitly authorized adult group routes."""

from __future__ import annotations

from dataclasses import dataclass
import re

from .ai_group_prompt import is_adult_content_config, sanitize_group_messages


ADULT_CONTENT_MIN_CHINESE_CHARACTERS = 8
ADULT_CONTENT_MAX_CHINESE_CHARACTERS = 20
CHINESE_CHARACTER = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
GENERIC_WARMUP_FORBIDDEN = (
    "附和", "推荐", "哪家", "哪里", "老师", "课代表", "位置", "地址",
    "公寓", "酒店", "服务", "体验", "预约", "交作业", "上次", "昨晚",
    "刚去", "去过", "试过",
)
FACT_MARKER_GROUPS = (
    ("experience", ("刚去", "去过", "试过", "昨晚去", "上次去", "之前去", "到店", "交作业")),
    ("location", ("公寓", "酒店", "商圈", "楼下", "停车", "路口", "地址", "位置")),
    ("service", ("服务", "态度", "配合度", "耐心", "催钟", "温柔", "靠谱")),
)


@dataclass(frozen=True)
class AdultContentViolation:
    code: str
    detail: str


def validate_adult_content_contract(
    config: dict,
    *,
    content: str,
    history: str,
    slot: dict | None = None,
) -> AdultContentViolation | None:
    if (config.get("ai_content_route_v2_enabled") and slot
            and slot.get("context_route") == slot.get("content_mode") == "general"):
        return None
    if not is_adult_content_config(config):
        return None
    chinese_count = len(CHINESE_CHARACTER.findall(content))
    if not ADULT_CONTENT_MIN_CHINESE_CHARACTERS <= chinese_count <= ADULT_CONTENT_MAX_CHINESE_CHARACTERS:
        return AdultContentViolation(
            "adult_content_length_out_of_range",
            f"chinese_character_count={chinese_count}",
        )
    context = " ".join(sanitize_group_messages(history.splitlines(), allow_adult_context=True))
    if not context:
        return _generic_warmup_violation(content)
    for group_name, markers in FACT_MARKER_GROUPS:
        if any(marker in content for marker in markers) and not any(marker in context for marker in markers):
            return AdultContentViolation("adult_content_fact_unanchored", f"fact_group={group_name}")
    return None


def _generic_warmup_violation(content: str) -> AdultContentViolation | None:
    if not content.rstrip().endswith(("？", "?")):
        return AdultContentViolation(
            "adult_generic_warmup_requires_question",
            "generic_warmup_must_be_question",
        )
    if marker := next((item for item in GENERIC_WARMUP_FORBIDDEN if item in content), ""):
        return AdultContentViolation(
            "adult_generic_warmup_scope_violation",
            f"forbidden_marker={marker}",
        )
    return None


__all__ = [
    "ADULT_CONTENT_MAX_CHINESE_CHARACTERS",
    "ADULT_CONTENT_MIN_CHINESE_CHARACTERS",
    "AdultContentViolation",
    "validate_adult_content_contract",
]

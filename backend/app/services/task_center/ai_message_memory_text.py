from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from functools import lru_cache
from hashlib import sha256


VAGUE_TEMPLATE_TERMS = ("确实", "感觉", "靠谱", "不错", "可以")
SPECIFIC_TEMPLATE_TERMS = (
    "价格", "多少", "怎么", "哪", "问", "照片", "位置", "反馈", "身材", "服务", "新妹子", "上榜", "药",
)
_COSMETIC_EMOJI = re.compile(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]+")
_REPEATED_PUNCT = re.compile(r"([!?！？。,.，、])\1+")
_SPACE = re.compile(r"\s+")
_MENTION = re.compile(r"@[a-z0-9_]{3,32}")
_VARIABLE_PERSON_LABEL = re.compile(r"[\u4e00-\u9fffa-z0-9_]{1,8}(老师|主任|哥|姐)")


@dataclass(frozen=True)
class _CharProfile:
    characters: frozenset[str]
    counts: tuple[tuple[str, int], ...]
    length: int


def normalize_group_ai_text(text: str) -> str:
    original = str(text or "").strip().lower()
    value = _COSMETIC_EMOJI.sub("", original)
    value = _SPACE.sub("", value)
    value = _collapse_variable_labels(value)
    value = _REPEATED_PUNCT.sub(r"\1", value)
    value = value.replace("！", "!").replace("？", "?").replace("，", ",").replace("。", ".")
    value = value.strip("!?.,;:，。！？；：、")
    if value:
        return value
    fallback = _SPACE.sub("", original)
    return _REPEATED_PUNCT.sub(r"\1", fallback).strip("!?.,;:，。！？；：、")


def text_similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    return max(SequenceMatcher(None, left, right).ratio(), _char_jaccard(left, right))


def text_similarity_reaches(left: str, right: str, threshold: float) -> bool:
    if not left or not right:
        return 0.0 >= threshold
    left_profile = _char_profile(left)
    right_profile = _char_profile(right)
    if _profile_jaccard(left_profile, right_profile) >= threshold:
        return True
    if _sequence_ratio_upper_bound(left_profile, right_profile) < threshold:
        return False
    return SequenceMatcher(None, left, right).ratio() >= threshold


def text_fingerprint(normalized: str) -> str:
    return sha256(normalized.encode("utf-8")).hexdigest()


def message_identity(raw_text: str) -> tuple[str, str, str, str]:
    normalized = normalize_group_ai_text(raw_text)
    return (
        normalized,
        text_fingerprint(normalized),
        semantic_cluster(normalized),
        template_shell_key(normalized),
    )


def semantic_cluster(normalized: str) -> str:
    chars = "".join(sorted(set(normalized)))
    return text_fingerprint(chars)[:24] if chars else ""


def template_shell_key(normalized: str) -> str:
    hits = [term for term in VAGUE_TEMPLATE_TERMS if term in normalized]
    if len(hits) >= 2 and not any(term in normalized for term in SPECIFIC_TEMPLATE_TERMS):
        return "vague-positive:generic"
    return ""


def reservation_key(
    tenant_id: int,
    fingerprint: str,
    now: datetime,
    window: timedelta,
) -> str:
    return f"{tenant_id}:all-groups:{fingerprint}:{int(now.timestamp()) // int(window.total_seconds())}"


def _collapse_variable_labels(value: str) -> str:
    value = _MENTION.sub("@user", value)
    return _VARIABLE_PERSON_LABEL.sub("<person>", value)


def _char_jaccard(left: str, right: str) -> float:
    return _profile_jaccard(_char_profile(left), _char_profile(right))


@lru_cache(maxsize=65_536)
def _char_profile(value: str) -> _CharProfile:
    return _CharProfile(frozenset(value), tuple(Counter(value).items()), len(value))


def _profile_jaccard(left: _CharProfile, right: _CharProfile) -> float:
    if not left.characters or not right.characters:
        return 0.0
    return len(left.characters & right.characters) / len(left.characters | right.characters)


def _sequence_ratio_upper_bound(left: _CharProfile, right: _CharProfile) -> float:
    right_counts = dict(right.counts)
    matches = sum(min(count, right_counts.get(char, 0)) for char, count in left.counts)
    return 2 * matches / (left.length + right.length)


__all__ = [
    "normalize_group_ai_text",
    "message_identity",
    "reservation_key",
    "semantic_cluster",
    "template_shell_key",
    "text_fingerprint",
    "text_similarity",
    "text_similarity_reaches",
]

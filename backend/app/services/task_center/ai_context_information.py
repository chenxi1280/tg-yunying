from __future__ import annotations

import re


MAX_SPEAKER_PREFIX_CHARS = 40
MIN_MEANINGFUL_CHINESE_CHARS = 2
_SPEAKER_PREFIX = re.compile(
    rf"^[^:：\n]{{1,{MAX_SPEAKER_PREFIX_CHARS}}}[:：]\s*"
)
_SPACE = re.compile(r"\s+")
_COMPACT = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff]+")
_ASCII_ONLY = re.compile(r"[0-9A-Za-z]+")
_LOW_INFORMATION = frozenset({
    "嗯",
    "哦",
    "啊",
    "哈",
    "哈哈",
    "呵呵",
    "在吗",
    "有人吗",
    "来了",
})


def meaningful_context_text(value: object) -> str:
    text = _SPACE.sub(" ", str(value or "")).strip()
    text = _SPEAKER_PREFIX.sub("", text).strip()
    compact = _COMPACT.sub("", text)
    if not compact or compact in _LOW_INFORMATION:
        return ""
    if _ASCII_ONLY.fullmatch(compact):
        return ""
    chinese_count = sum("\u4e00" <= char <= "\u9fff" for char in compact)
    return text if chinese_count >= MIN_MEANINGFUL_CHINESE_CHARS else ""


def meaningful_context_lines(values: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        text
        for value in values
        if (text := meaningful_context_text(value))
    )


def meaningful_group_evidence(
    history: str,
    topic_direction: object,
    adult_markers: tuple[str, ...],
) -> tuple[str, ...]:
    history_lines = meaningful_context_lines(tuple(str(history or "").splitlines()))
    if history_lines:
        return history_lines
    topic = _topic_title(topic_direction)
    if not topic or any(marker in topic for marker in adult_markers):
        return ()
    return (f"群话题：{topic}",)


def general_topic_lines(slots: list[dict]) -> list[str]:
    return [
        f"群话题：{title}"
        for slot in slots
        if str(slot.get("content_mode") or "") == "general"
        if (title := _topic_title(slot.get("topic_direction")))
    ]


def _topic_title(value: object) -> str:
    raw = value.get("title") if isinstance(value, dict) else ""
    return meaningful_context_text(raw)


__all__ = [
    "general_topic_lines",
    "meaningful_context_lines",
    "meaningful_context_text",
    "meaningful_group_evidence",
]

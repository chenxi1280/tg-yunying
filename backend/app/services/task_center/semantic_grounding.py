from __future__ import annotations

import re

from .message_brief import MessageBrief, normalize_text
from .message_brief_v2 import MessageBriefV2, v2_candidate_failure


_COMPACT_TEXT = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff]+")
_UNSUPPORTED_CLAIM_MARKERS = {
    "experience": ("我去过", "我用过", "我试过", "我买过", "亲自体验"),
    "location": ("在我这", "我这边", "附近", "地址", "位于"),
    "transaction": ("下单", "成交", "付款", "已经买", "刚买"),
    "price": ("¥", "￥", "元", "块钱", "价格", "多少钱"),
}


def lexical_grounding_evidence(
    content: str,
    brief: MessageBrief,
    *,
    used_anchor_ids: list[str],
    anchor_texts: dict[str, str],
    reply_preview: str = "",
) -> dict:
    evidence_texts = [
        normalize_text(anchor_texts.get(anchor_id))
        for anchor_id in used_anchor_ids
        if normalize_text(anchor_texts.get(anchor_id))
    ]
    if reply_preview.strip():
        evidence_texts.append(normalize_text(reply_preview))
    v2_failure = (
        v2_candidate_failure(content, brief, tuple(evidence_texts))
        if isinstance(brief, MessageBriefV2)
        else ""
    )
    unsupported = "" if isinstance(brief, MessageBriefV2) else _unsupported_claim(
        content,
        evidence_texts,
        brief.forbidden_claims,
    )
    matches = _matching_units(content, evidence_texts)
    return {
        "collector_version": "lexical_grounding_v1",
        "used_anchor_ids": list(used_anchor_ids),
        "matched_units": matches,
        "unsupported_claim_marker": unsupported,
        "failure_code": v2_failure,
    }


def _unsupported_claim(
    content: str,
    evidence_texts: list[str],
    forbidden_claims: tuple[str, ...],
) -> str:
    evidence = " ".join(evidence_texts)
    for claim in forbidden_claims:
        markers = _UNSUPPORTED_CLAIM_MARKERS.get(claim, ())
        if any(marker in content and marker not in evidence for marker in markers):
            return claim
    return ""


def _matching_units(content: str, evidence_texts: list[str]) -> list[str]:
    content_units = _semantic_units(content)
    evidence_units: set[str] = set()
    for text in evidence_texts:
        evidence_units.update(_semantic_units(text))
    return sorted(content_units & evidence_units)[:8]


def _semantic_units(value: str) -> set[str]:
    compact = _COMPACT_TEXT.sub("", normalize_text(value)).lower()
    if len(compact) < 2:
        return {compact} if compact else set()
    return {compact[index:index + 2] for index in range(len(compact) - 1)}


__all__ = ["lexical_grounding_evidence"]

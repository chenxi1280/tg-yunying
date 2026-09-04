from __future__ import annotations

import hashlib
import re
from typing import Sequence
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ReactionIntentPolicyRevision, SourceReactionIntentDecision, Task, TgAccount
from app.services._common import _now

EMOJI_INTENT_MAP: dict[str, str] = {
    # positive
    "👍": "positive",
    "❤️": "positive",
    "❤": "positive",
    "🥰": "positive",
    "😍": "positive",
    # support
    "🙏": "support",
    "🤝": "support",
    "💪": "support",
    "🫡": "support",
    # celebrate
    "🎉": "celebrate",
    "🚀": "celebrate",
    "🔥": "celebrate",
    "🍾": "celebrate",
    "🏆": "celebrate",
    # neutral
    "👀": "neutral",
    "👏": "neutral",
    "🤔": "neutral",
    "👌": "neutral",
}

DEFAULT_SAFE_INTENTS = frozenset({"positive", "support"})

DEFAULT_NEGATIVE_KEYWORDS = frozenset({
    "悼念", "黑客", "亏损", "维权", "被盗", "暂停运营", "故障维护",
    "跑路", "归零", "清算", "攻击", "下架", "受害", "出事", "诈骗", "崩盘", "被抓", "关停",
})


def normalize_emoji(emoji: str) -> str:
    return str(emoji or "").replace("\ufe0f", "").replace("\ufe0e", "").strip()


def classify_emoji_intent(emoji: str) -> str:
    key = normalize_emoji(emoji)
    return EMOJI_INTENT_MAP.get(key, "unknown")


def detect_negative_keywords(
    text: str,
    keywords: Sequence[str] | None = None,
) -> bool:
    if not text:
        return False
    norm_text = str(text).lower()
    targets = keywords or DEFAULT_NEGATIVE_KEYWORDS
    for kw in targets:
        if kw.lower() in norm_text:
            return True
    return False


def resolve_safe_reactions(
    configured: list[str],
    available: list[str],
    *,
    content_text: str = "",
    allow_celebrate_if_clean: bool = True,
    safe_intents: set[str] | None = None,
    negative_keywords: Sequence[str] | None = None,
) -> tuple[list[str], str, dict]:
    """
    Compute: channel.allowed ∩ task.configured ∩ safe_intent_reactions.
    If negative keywords detected, strictly forbid celebrate intent.
    """
    has_negative = detect_negative_keywords(content_text, negative_keywords)
    available_map = {normalize_emoji(item): item for item in available}
    configured_map = {normalize_emoji(item): item for item in configured}

    candidate_reactions: list[str] = []
    search_keys = list(configured_map.keys()) if configured_map else list(available_map.keys())

    if safe_intents is not None:
        effective_intents = set(safe_intents)
        if allow_celebrate_if_clean and not has_negative:
            effective_intents.add("celebrate")
        elif has_negative and "celebrate" in effective_intents:
            effective_intents.remove("celebrate")

        for key in search_keys:
            if key in available_map:
                intent = classify_emoji_intent(key)
                if intent in effective_intents:
                    candidate_reactions.append(available_map[key])
    else:
        # No explicit safe_intents constraint: keep all candidate emojis,
        # but if negative keywords detected, remove celebrate emojis!
        effective_intents = set()
        for key in search_keys:
            if key in available_map:
                intent = classify_emoji_intent(key)
                if has_negative and intent == "celebrate":
                    continue
                candidate_reactions.append(available_map[key])

    details = {
        "has_negative_keywords": has_negative,
        "effective_safe_intents": sorted(list(effective_intents)),
        "candidate_count": len(candidate_reactions),
    }

    if not available:
        return [], "reaction_capability_blocked", details
    if not candidate_reactions:
        common_keys = set(configured_map.keys()) & set(available_map.keys()) if configured_map else set(available_map.keys())
        if not common_keys:
            return [], "reaction_capability_blocked", details
        return [], "reaction_intent_no_match", details

    return candidate_reactions, "confirmed", details


def ensure_reaction_intent_policy(
    session: Session,
    tenant_id: int,
) -> ReactionIntentPolicyRevision:
    policy = session.scalar(
        select(ReactionIntentPolicyRevision).where(
            ReactionIntentPolicyRevision.tenant_id == tenant_id,
            ReactionIntentPolicyRevision.state == "active",
        )
    )
    if policy is not None:
        return policy
    policy = ReactionIntentPolicyRevision(
        tenant_id=tenant_id,
        revision=1,
        state="active",
        intent_mappings=dict(EMOJI_INTENT_MAP),
        safe_intents=sorted(list(DEFAULT_SAFE_INTENTS)),
        negative_keywords=sorted(list(DEFAULT_NEGATIVE_KEYWORDS)),
    )
    session.add(policy)
    session.flush()
    return policy


def evaluate_source_reaction_intent(
    session: Session,
    *,
    task: Task,
    account: TgAccount,
    source_revision: str,
    content_text: str,
    allowed_reactions: list[str],
    configured_reactions: list[str],
    reaction_scope: str = "configured",
) -> SourceReactionIntentDecision:
    policy = ensure_reaction_intent_policy(session, task.tenant_id)
    content_hash = hashlib.sha256((content_text or "").encode("utf-8")).hexdigest()

    existing = session.scalar(
        select(SourceReactionIntentDecision).where(
            SourceReactionIntentDecision.task_id == task.id,
            SourceReactionIntentDecision.account_id == account.id,
            SourceReactionIntentDecision.source_revision == source_revision,
            SourceReactionIntentDecision.policy_revision_id == policy.id,
        )
    )
    if existing is not None:
        return existing

    cfg = configured_reactions if reaction_scope != "all_available" else []
    candidates, decision, details = resolve_safe_reactions(
        cfg,
        allowed_reactions,
        content_text=content_text,
        safe_intents=set(policy.safe_intents or DEFAULT_SAFE_INTENTS),
        negative_keywords=policy.negative_keywords or list(DEFAULT_NEGATIVE_KEYWORDS),
    )

    detected_intents = sorted(list({classify_emoji_intent(c) for c in candidates}))
    chosen = candidates[0] if (decision == "confirmed" and candidates) else ""

    record = SourceReactionIntentDecision(
        tenant_id=task.tenant_id,
        task_id=task.id,
        account_id=account.id,
        source_revision=source_revision,
        policy_revision_id=policy.id,
        content_hash=content_hash,
        detected_intents=detected_intents,
        has_negative_keywords=details.get("has_negative_keywords", False),
        allowed_reactions=allowed_reactions,
        configured_reactions=configured_reactions,
        candidate_reactions=candidates,
        chosen_reaction=chosen,
        decision=decision,
        reason="" if decision == "confirmed" else decision,
    )
    session.add(record)
    session.flush()
    return record


__all__ = [
    "DEFAULT_NEGATIVE_KEYWORDS",
    "DEFAULT_SAFE_INTENTS",
    "EMOJI_INTENT_MAP",
    "classify_emoji_intent",
    "detect_negative_keywords",
    "ensure_reaction_intent_policy",
    "evaluate_source_reaction_intent",
    "normalize_emoji",
    "resolve_safe_reactions",
]

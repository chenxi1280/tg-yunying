from __future__ import annotations

from dataclasses import dataclass
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ContentKeywordRule, TgGroup


@dataclass(frozen=True)
class ContentFilterResult:
    ok: bool
    content: str
    reason: str = ""


def split_rule_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [item.strip() for item in re.split(r"[\n,，;；]+", raw) if item.strip()]


def extract_links(text: str) -> list[str]:
    return re.findall(r"(https?://\S+|www\.\S+)", text, flags=re.IGNORECASE)


def tenant_keyword_rules(session: Session, tenant_id: int) -> list[ContentKeywordRule]:
    return list(
        session.scalars(
            select(ContentKeywordRule)
            .where(ContentKeywordRule.tenant_id == tenant_id, ContentKeywordRule.is_active.is_(True))
            .order_by(ContentKeywordRule.id.asc())
        )
    )


def _looks_like_reply(text: str) -> bool:
    stripped = text.strip()
    lowered = stripped.lower()
    return (
        stripped.startswith(">")
        or stripped.startswith("↩")
        or lowered.startswith("reply to")
        or lowered.startswith("re:")
        or stripped.startswith("回复")
        or stripped.startswith("引用")
    )


def _hit_keyword(text: str, rules: list[ContentKeywordRule]) -> str | None:
    lowered = text.lower()
    for rule in rules:
        keyword = rule.keyword.strip()
        if not keyword:
            continue
        if rule.match_type != "contains":
            continue
        if keyword.lower() in lowered:
            return keyword
    return None


def filter_outbound_content(
    session: Session,
    *,
    tenant_id: int,
    group: TgGroup,
    content: str,
    reject_mentions: bool = False,
    reject_replies: bool = False,
) -> ContentFilterResult:
    cleaned = re.sub(r"\s+", " ", str(content or "")).strip()
    if not cleaned:
        return ContentFilterResult(False, "", "内容为空")
    if reject_replies and _looks_like_reply(cleaned):
        return ContentFilterResult(False, "", "过滤回复消息")
    if reject_mentions and "@" in cleaned:
        return ContentFilterResult(False, "", "过滤@或提及")

    keyword = _hit_keyword(cleaned, tenant_keyword_rules(session, tenant_id))
    if keyword:
        return ContentFilterResult(False, "", f"命中租户关键词：{keyword}")

    banned_words = split_rule_list(group.banned_words)
    hit_words = [word for word in banned_words if word and word in cleaned]
    if hit_words:
        return ContentFilterResult(False, "", f"命中群禁词：{'、'.join(hit_words[:3])}")

    whitelist = split_rule_list(group.link_whitelist)
    links = extract_links(cleaned)
    if whitelist and links:
        for link in links:
            normalized = link.lower()
            if not any(rule.lower() in normalized for rule in whitelist):
                return ContentFilterResult(False, "", f"链接不在白名单内：{link}")
    return ContentFilterResult(True, cleaned[:2000], "")


__all__ = ["ContentFilterResult", "extract_links", "filter_outbound_content", "split_rule_list", "tenant_keyword_rules"]

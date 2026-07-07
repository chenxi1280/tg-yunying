from __future__ import annotations

from dataclasses import dataclass
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ContentKeywordRule, TgGroup


GENERATED_TEMPLATE_MARKERS = (
    "刚看到大家提到",
    "刚看到有人聊这个",
    "看大家聊",
    "顺着这个话题说",
    "顺着刚才说的",
    "这个点挺有意思",
    "这个点我也留意到了",
    "可以继续聊聊",
    "感觉可以继续聊聊",
    "大家怎么看",
    "有经验的朋友也可以补充下",
    "这个方向可以展开一下",
    "这个话题",
    "自然接一句",
    "换个角度",
    "轻量推进",
    "值得讨论",
)

AI_META_MARKERS = (
    "<think>",
    "</think>",
    "让我分析",
    "我来分析",
    "我先分析",
    "仔细分析这个请求",
    "分析这个频道内容",
    "这是一个要求生成",
    "这是一个生成",
)

AI_META_PATTERNS = (
    re.compile(r"^\s*(?:好的|可以|明白)[，,\s]*(?:我会|我来|让我)", re.IGNORECASE),
    re.compile(r"^\s*(?:as an ai|i need to analyze|let me analyze)\b", re.IGNORECASE),
    re.compile(r"^\s*这是?(?:一个|一段)?明显.*(?:色情|敏感|违规|请求|任务|频道|内容)"),
)

OPERATOR_UI_MARKERS = (
    "点击底部按钮",
    "点击查看",
    "请点下面按钮",
    "还没有你的定位",
    "为了保护隐私",
    "更新后回到本群",
    "查询附近老师",
    "积分商城",
    "提交报告",
    "约课记录",
    "口令现金",
)

COARSE_LANGUAGE_MARKERS = (
    "傻逼",
    "傻b",
    "煞笔",
    "沙币",
    "妈的",
    "他妈的",
    "妈了个",
    "卧槽",
    "我操",
    "操你",
    "草你",
    "艹你",
)
COARSE_LANGUAGE_PATTERNS = (
    re.compile(r"\b(?:cnm|nmsl|fuck|shit)\b", re.IGNORECASE),
)


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


def _looks_like_internal_prompt(text: str) -> bool:
    markers = (
        "当前群暂无可用历史消息",
        "不要提到系统、任务或 AI",
        "不要提到系统、任务或AI",
        "生成自然开场",
        "只输出 JSON",
        "risk_level",
        "persona",
        "刚看到大家提到“刚看到大家提到",
        "[已撤回的内部提示词",
    )
    return any(marker in text for marker in markers)


def looks_like_generated_template_noise(text: str) -> bool:
    cleaned = str(text or "")
    return any(marker in cleaned for marker in GENERATED_TEMPLATE_MARKERS)


def looks_like_ai_meta_content(text: str) -> bool:
    cleaned = str(text or "").strip()
    lowered = cleaned.lower()
    marker_hit = any(marker.lower() in lowered for marker in AI_META_MARKERS)
    pattern_hit = any(pattern.search(cleaned) for pattern in AI_META_PATTERNS)
    return marker_hit or pattern_hit


def looks_like_operator_ui_content(text: str) -> bool:
    cleaned = str(text or "")
    return any(marker in cleaned for marker in OPERATOR_UI_MARKERS)


def contains_coarse_language(text: str) -> bool:
    cleaned = str(text or "")
    lowered = cleaned.lower()
    marker_hit = any(marker in lowered for marker in COARSE_LANGUAGE_MARKERS)
    pattern_hit = any(pattern.search(cleaned) for pattern in COARSE_LANGUAGE_PATTERNS)
    return marker_hit or pattern_hit


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
    if _looks_like_internal_prompt(cleaned):
        return ContentFilterResult(False, "", "拦截内部提示词")
    if looks_like_ai_meta_content(cleaned):
        return ContentFilterResult(False, "", "拦截 AI 过程性内容")
    if looks_like_generated_template_noise(cleaned):
        return ContentFilterResult(False, "", "拦截模板化生成内容")
    if looks_like_operator_ui_content(cleaned):
        return ContentFilterResult(False, "", "拦截后台/按钮说明内容")
    if contains_coarse_language(cleaned):
        return ContentFilterResult(False, "", "命中粗俗表达")
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


def rewrite_rejected_content(session: Session, *, tenant_id: int, group: TgGroup, content: str) -> ContentFilterResult:
    cleaned = re.sub(r"\s+", " ", str(content or "")).strip()
    for rule in tenant_keyword_rules(session, tenant_id):
        keyword = rule.keyword.strip()
        if keyword:
            cleaned = re.sub(re.escape(keyword), "", cleaned, flags=re.IGNORECASE)
    for word in split_rule_list(group.banned_words):
        if word:
            cleaned = cleaned.replace(word, "")
    whitelist = split_rule_list(group.link_whitelist)
    if whitelist:
        for link in extract_links(cleaned):
            normalized = link.lower()
            if not any(rule.lower() in normalized for rule in whitelist):
                cleaned = cleaned.replace(link, "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ，,。；;、")
    if cleaned == str(content or "").strip():
        return ContentFilterResult(False, "", "内容未产生可用改写")
    return filter_outbound_content(session, tenant_id=tenant_id, group=group, content=cleaned)


__all__ = [
    "ContentFilterResult",
    "contains_coarse_language",
    "extract_links",
    "filter_outbound_content",
    "looks_like_ai_meta_content",
    "looks_like_generated_template_noise",
    "looks_like_operator_ui_content",
    "rewrite_rejected_content",
    "split_rule_list",
    "tenant_keyword_rules",
]

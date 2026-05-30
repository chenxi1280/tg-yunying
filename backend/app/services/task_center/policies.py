from __future__ import annotations

import re

from sqlalchemy.orm import Session

from app.models import FailureType, GroupAuthStatus, TgGroup
from app.services.content_filters import tenant_keyword_rules


def _split_rule_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [item.strip() for item in re.split(r"[\n,，;；]+", raw) if item.strip()]


def _extract_links(text: str) -> list[str]:
    return re.findall(r"(https?://\S+|www\.\S+)", text, flags=re.IGNORECASE)


def validate_group_send_policy(session: Session, *, tenant_id: int, group: TgGroup, content: str, review_approved: bool) -> tuple[str | None, str | None]:
    if group.auth_status != GroupAuthStatus.AUTHORIZED.value:
        return FailureType.GROUP_PERMISSION_DENIED.value, "群未授权运营"
    if not group.can_send:
        return FailureType.GROUP_PERMISSION_DENIED.value, "群当前不可发送"
    tenant_hit = next((rule.keyword for rule in tenant_keyword_rules(session, tenant_id) if rule.keyword and rule.keyword.lower() in content.lower()), None)
    if tenant_hit:
        return FailureType.CONTENT_REJECTED.value, f"命中租户关键词：{tenant_hit}"
    hit_words = [word for word in _split_rule_list(group.banned_words) if word and word in content]
    if hit_words:
        return FailureType.CONTENT_REJECTED.value, f"命中群禁词：{'、'.join(hit_words[:3])}"
    whitelist = _split_rule_list(group.link_whitelist)
    if whitelist:
        for link in _extract_links(content):
            normalized = link.lower()
            if not any(rule.lower() in normalized for rule in whitelist):
                return FailureType.CONTENT_REJECTED.value, f"链接不在白名单内：{link}"
    return None, None


__all__ = ["validate_group_send_policy"]

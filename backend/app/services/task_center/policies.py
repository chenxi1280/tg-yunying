from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Action, FailureType, GroupAuthStatus, MessageTask, TaskStatus, TgGroup
from app.services._common import _as_utc, _now
from app.services.content_filters import tenant_keyword_rules


def _utc_day_bounds(value: datetime | None = None) -> tuple[datetime, datetime]:
    current = (value or _now()).replace(tzinfo=UTC) if (value or _now()).tzinfo is None else (value or _now()).astimezone(UTC)
    start = current.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return start.replace(tzinfo=None), end.replace(tzinfo=None)


def _split_rule_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [item.strip() for item in re.split(r"[\n,，;；]+", raw) if item.strip()]


def _extract_links(text: str) -> list[str]:
    return re.findall(r"(https?://\S+|www\.\S+)", text, flags=re.IGNORECASE)


def _task_center_group_sent_today(session: Session, tenant_id: int, group_id: int) -> int:
    day_start, day_end = _utc_day_bounds()
    return session.scalar(
        select(func.count(Action.id)).where(
            Action.tenant_id == tenant_id,
            Action.action_type == "send_message",
            Action.status == "success",
            Action.executed_at.is_not(None),
            Action.executed_at >= day_start,
            Action.executed_at < day_end,
            Action.payload["group_id"].as_integer() == group_id,
        )
    ) or 0


def _legacy_group_sent_today(session: Session, tenant_id: int, group_id: int) -> int:
    day_start, day_end = _utc_day_bounds()
    return session.scalar(
        select(func.count(MessageTask.id)).where(
            MessageTask.tenant_id == tenant_id,
            MessageTask.group_id == group_id,
            MessageTask.status == TaskStatus.SENT.value,
            MessageTask.sent_at.is_not(None),
            MessageTask.sent_at >= day_start,
            MessageTask.sent_at < day_end,
        )
    ) or 0


def _group_sent_today(session: Session, tenant_id: int, group_id: int) -> int:
    return int(_task_center_group_sent_today(session, tenant_id, group_id)) + int(_legacy_group_sent_today(session, tenant_id, group_id))


def _task_center_group_last_sent_at(session: Session, tenant_id: int, group_id: int) -> datetime | None:
    return session.scalar(
        select(func.max(Action.executed_at)).where(
            Action.tenant_id == tenant_id,
            Action.action_type == "send_message",
            Action.status == "success",
            Action.executed_at.is_not(None),
            Action.payload["group_id"].as_integer() == group_id,
        )
    )


def _legacy_group_last_sent_at(session: Session, tenant_id: int, group_id: int) -> datetime | None:
    return session.scalar(
        select(func.max(MessageTask.sent_at)).where(
            MessageTask.tenant_id == tenant_id,
            MessageTask.group_id == group_id,
            MessageTask.status == TaskStatus.SENT.value,
            MessageTask.sent_at.is_not(None),
        )
    )


def _group_last_sent_at(session: Session, tenant_id: int, group_id: int) -> datetime | None:
    candidates = [
        value
        for value in (
            _task_center_group_last_sent_at(session, tenant_id, group_id),
            _legacy_group_last_sent_at(session, tenant_id, group_id),
        )
        if value is not None
    ]
    return max(candidates, key=_as_utc) if candidates else None


def validate_group_send_policy(session: Session, *, tenant_id: int, group: TgGroup, content: str, review_approved: bool) -> tuple[str | None, str | None]:
    if group.auth_status != GroupAuthStatus.AUTHORIZED.value:
        return FailureType.GROUP_PERMISSION_DENIED.value, "群未授权运营"
    if not group.can_send:
        return FailureType.GROUP_PERMISSION_DENIED.value, "群当前不可发送"
    sent_today = _group_sent_today(session, tenant_id, group.id)
    if sent_today >= group.daily_limit:
        return FailureType.SLOWMODE.value, f"群当日发送已达上限 {group.daily_limit}"
    last_sent_at = _group_last_sent_at(session, tenant_id, group.id)
    if last_sent_at and (_as_utc(_now()) - _as_utc(last_sent_at)).total_seconds() < group.group_cooldown_seconds:
        return FailureType.SLOWMODE.value, f"群冷却中，还需等待 {group.group_cooldown_seconds} 秒"
    if group.require_review and not review_approved:
        return FailureType.CONTENT_REJECTED.value, "该群要求先审核后再发送"
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

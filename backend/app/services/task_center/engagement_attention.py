from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ConversationEvent, Task, TgGroup
from app.services._common import _now


DEFAULT_QUIET_MIN_SECONDS = 60
DEFAULT_QUIET_MAX_SECONDS = 180


def apply_proactive_quiet_windows(
    session: Session,
    task: Task,
    group: TgGroup,
    config: dict,
    items: list[dict],
    *,
    now_value: datetime | None = None,
) -> list[dict]:
    event = _latest_human_event(session, task, group)
    if event is None:
        return items
    current = _naive(now_value or _now())
    return [
        _with_quiet_window(task, event, config, item, index, current)
        for index, item in enumerate(items)
    ]


def latest_proactive_quiet_until(
    session: Session,
    task: Task,
    group: TgGroup,
    config: dict,
    *,
    identity: str,
) -> datetime | None:
    event = _latest_human_event(session, task, group)
    if event is None:
        return None
    return _quiet_until(task, event, config, identity)


def _with_quiet_window(
    task: Task,
    event: ConversationEvent,
    config: dict,
    item: dict,
    index: int,
    current: datetime,
) -> dict:
    if item.get("reply_target"):
        return item
    identity = str(item.get("slot_id") or item.get("quality_slot_id") or index)
    quiet_until = _quiet_until(task, event, config, identity)
    if quiet_until <= current:
        return item
    return {**item, "proactive_quiet_until_at": quiet_until.isoformat()}


def _latest_human_event(
    session: Session,
    task: Task,
    group: TgGroup,
) -> ConversationEvent | None:
    if not _unified_group_task(task):
        return None
    return session.scalar(
        select(ConversationEvent)
        .where(
            ConversationEvent.tenant_id == task.tenant_id,
            ConversationEvent.surface == "group_ai_chat",
            ConversationEvent.canonical_peer_id == str(group.tg_peer_id),
            ConversationEvent.author_class == "external_human",
            ConversationEvent.is_current.is_(True),
            ConversationEvent.deleted_at.is_(None),
        )
        .order_by(ConversationEvent.sent_at.desc(), ConversationEvent.id.desc())
        .limit(1)
    )


def _quiet_until(
    task: Task,
    event: ConversationEvent,
    config: dict,
    identity: str,
) -> datetime:
    minimum, maximum = _quiet_range(config)
    span = maximum - minimum + 1
    seed = f"{task.id}:{event.id}:{identity}".encode("utf-8")
    delay = minimum + int.from_bytes(hashlib.sha256(seed).digest()[:8], "big") % span
    return _naive(event.sent_at) + timedelta(seconds=delay)


def _quiet_range(config: dict) -> tuple[int, int]:
    minimum = int(config.get("attention_quiet_after_min_seconds") or DEFAULT_QUIET_MIN_SECONDS)
    maximum = int(config.get("attention_quiet_after_max_seconds") or DEFAULT_QUIET_MAX_SECONDS)
    if minimum < 0 or maximum < minimum:
        raise ValueError("attention_quiet_after_range_invalid")
    return minimum, maximum


def _unified_group_task(task: Task) -> bool:
    return (
        getattr(task, "type", "") == "group_ai_chat"
        and (getattr(task, "type_config", None) or {}).get("engagement_contract_version")
        == "unified_engagement_v1"
    )


def _naive(value: datetime) -> datetime:
    return value.replace(tzinfo=None) if value.tzinfo else value


__all__ = ["apply_proactive_quiet_windows", "latest_proactive_quiet_until"]

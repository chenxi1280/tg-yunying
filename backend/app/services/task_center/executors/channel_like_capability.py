from __future__ import annotations

import hashlib
import json

from sqlalchemy.orm import Session

from app.models import ChannelMessage, ChannelMessageSourceRevision, OperationTarget, Task

from .channel_like_reactions import reaction_plan


def message_reaction_plan(
    session: Session,
    task: Task,
    message: ChannelMessage,
    *,
    config: dict,
    reactions: list[str],
    quantity: int,
    seed_id: str,
) -> list[str]:
    plan = reaction_plan(
        reactions,
        quantity,
        str(config.get("reaction_type") or "random"),
        seed_id=seed_id,
        reaction_scope=str(config.get("reaction_scope") or "all_available"),
        available_reactions=_available_reactions(session, message),
        reaction_capability_mode=_capability_mode(session, message),
        content_text=reaction_source_text(session, message),
    )
    if plan:
        clear_reaction_capability_block(task, message.id)
    else:
        _record_reaction_capability_block(session, task, message, config=config, reactions=reactions)
    return plan


def reaction_capability_revision(target: OperationTarget) -> str:
    capability = {
        "mode": str(target.reaction_capability_mode or "unknown"),
        "available_reactions": sorted(
            _normalized_reactions(list(target.available_reactions or []))
        ),
    }
    encoded = json.dumps(
        capability, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def target_accepts_reaction(target: OperationTarget, reaction: str, *, content_text: str = "") -> bool:
    return bool(reaction_plan(
        [reaction],
        1,
        "specific",
        available_reactions=list(target.available_reactions or []),
        reaction_capability_mode=str(target.reaction_capability_mode or "unknown"),
        content_text=content_text,
    ))


def reaction_source_text(session: Session, message: ChannelMessage) -> str:
    if not message.current_source_revision_id:
        return str(message.content_preview or "")
    revision = session.get(ChannelMessageSourceRevision, message.current_source_revision_id)
    if revision is None or revision.channel_message_id != message.id:
        raise RuntimeError("reaction_source_revision_stale")
    return str(revision.source_text_snapshot or "")


def _normalized_reactions(reactions: list[str]) -> list[str]:
    return [
        str(value).replace("\ufe0f", "").replace("\ufe0e", "").strip()
        for value in reactions
        if str(value).strip()
    ]


def clear_reaction_capability_block(task: Task, message_id: int) -> None:
    stats = dict(task.stats or {})
    message_ids = {
        int(value)
        for value in stats.get("reaction_capability_unavailable_message_ids", [])
        if int(value) != message_id
    }
    if message_ids:
        stats["reaction_capability_unavailable_message_ids"] = sorted(message_ids)
    else:
        stats.pop("reaction_capability_unavailable_message_ids", None)
        stats.pop("reaction_capability_unavailable", None)
        if task.last_error.startswith("Reaction 能力不可用或无有效交集"):
            task.last_error = ""
    task.stats = stats


def _record_reaction_capability_block(
    session: Session,
    task: Task,
    message: ChannelMessage,
    *,
    config: dict,
    reactions: list[str],
) -> None:
    mode = _capability_mode(session, message)
    capability_plan = reaction_plan(
        reactions, 1,
        str(config.get("reaction_type") or "random"),
        reaction_scope=str(config.get("reaction_scope") or "all_available"),
        available_reactions=_available_reactions(session, message),
        reaction_capability_mode=mode,
    )
    reason = "reaction_intent_no_match" if capability_plan else "reaction_capability_unavailable"
    stats = dict(task.stats or {})
    message_ids = {int(value) for value in stats.get("reaction_capability_unavailable_message_ids", [])}
    message_ids.add(message.id)
    stats["reaction_capability_unavailable_message_ids"] = sorted(message_ids)
    stats["reaction_capability_unavailable"] = {
        "reason_code": reason,
        "channel_message_id": message.id,
        "capability_mode": mode,
        "reaction_scope": str(config.get("reaction_scope") or "all_available"),
    }
    task.stats = stats
    probe = stats.get("reaction_capability_probe") or {}
    if reason == "reaction_intent_no_match":
        task.last_error = f"Reaction 能力不可用或无有效交集：语义不匹配，message={message.id}"
    elif mode == "unknown" and probe.get("error_code"):
        task.last_error = f"Reaction 能力探测失败：{probe['error_code']}"
    elif mode == "none":
        task.last_error = "Reaction 能力不可用或无有效交集：频道未开放点赞表情"
    else:
        task.last_error = f"Reaction 能力不可用或无有效交集：message={message.id}, mode={mode}"


def _available_reactions(session: Session, message: ChannelMessage) -> list[str]:
    channel = session.get(OperationTarget, message.channel_target_id)
    return list(channel.available_reactions or []) if channel else []


def _capability_mode(session: Session, message: ChannelMessage) -> str:
    channel = session.get(OperationTarget, message.channel_target_id)
    return str(channel.reaction_capability_mode or "unknown") if channel else "unknown"

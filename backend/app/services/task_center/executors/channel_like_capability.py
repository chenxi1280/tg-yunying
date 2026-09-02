from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import ChannelMessage, OperationTarget, Task

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
    )
    if plan:
        clear_reaction_capability_block(task, message.id)
    else:
        _record_reaction_capability_block(session, task, message, config=config)
    return plan


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
) -> None:
    mode = _capability_mode(session, message)
    stats = dict(task.stats or {})
    message_ids = {int(value) for value in stats.get("reaction_capability_unavailable_message_ids", [])}
    message_ids.add(message.id)
    stats["reaction_capability_unavailable_message_ids"] = sorted(message_ids)
    stats["reaction_capability_unavailable"] = {
        "reason_code": "reaction_capability_unavailable",
        "channel_message_id": message.id,
        "capability_mode": mode,
        "reaction_scope": str(config.get("reaction_scope") or "all_available"),
    }
    task.stats = stats
    probe = stats.get("reaction_capability_probe") or {}
    if mode == "unknown" and probe.get("error_code"):
        task.last_error = f"Reaction 能力探测失败：{probe['error_code']}"
    else:
        task.last_error = f"Reaction 能力不可用或无有效交集：message={message.id}, mode={mode}"


def _available_reactions(session: Session, message: ChannelMessage) -> list[str]:
    channel = session.get(OperationTarget, message.channel_target_id)
    return list(channel.available_reactions or []) if channel else []


def _capability_mode(session: Session, message: ChannelMessage) -> str:
    channel = session.get(OperationTarget, message.channel_target_id)
    return str(channel.reaction_capability_mode or "unknown") if channel else "unknown"

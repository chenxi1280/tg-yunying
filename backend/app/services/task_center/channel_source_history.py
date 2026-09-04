"""Task-specific initial history proof, independent of shared snapshot freshness."""
from datetime import datetime

from sqlalchemy import select

from app.models import ChannelSourcePageCursor, ChannelTaskIntake
from app.timezone import as_beijing


DEFAULT_HISTORY_LIMIT = 5


def history_requirement(task):
    config = task.type_config or {}
    scope = config.get("initial_message_scope") or config.get("message_scope")
    if config.get("engagement_contract_version") != "unified_engagement_v1" or scope == "specific":
        return None
    raw = (task.stats or {}).get("started_at")
    anchor = as_beijing(datetime.fromisoformat(raw)) if raw else as_beijing(task.scheduled_start or task.created_at)
    limit = 0 if scope == "new_only" else int(config.get("initial_historical_post_limit", DEFAULT_HISTORY_LIMIT))
    return {"task_id": task.id, "epoch": task.task_lifecycle_epoch, "task_type": task.type,
        "anchor": anchor.isoformat(), "limit": limit}


def history_initialized(session, task, channel_target_id):
    return session.scalar(select(ChannelTaskIntake.id).where(ChannelTaskIntake.task_id == task.id,
        ChannelTaskIntake.lifecycle_epoch == task.task_lifecycle_epoch,
        ChannelTaskIntake.channel_target_id == channel_target_id)) is not None


def completed_history_matches(row, identity):
    return bool(row.get("complete") and all(row.get(key) == value for key, value in identity.items()))


def initial_history_ready(session, task, *, state):
    identity = history_requirement(task)
    if identity is None or history_initialized(session, task, int(state.source_peer_id)):
        return True
    cursor = session.get(ChannelSourcePageCursor, state.id)
    completed = (cursor.page_state or {}).get("completed_history", []) if cursor else []
    return any(completed_history_matches(row, identity) for row in completed)

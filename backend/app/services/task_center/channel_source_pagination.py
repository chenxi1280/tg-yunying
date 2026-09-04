"""One page per listener cycle; resume gaps and prove the initial logical backlog."""
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select

from app.models import ChannelSourcePageCursor, Task
from app.timezone import as_beijing
from .channel_source_policy import logical_source_key, source_filter_reason
from .channel_source_history import completed_history_matches, history_initialized, history_requirement


@dataclass(frozen=True)
class SourcePageProgress:
    complete: bool
    observed_at: datetime


def source_page_offset(session, state_id):
    cursor = session.get(ChannelSourcePageCursor, state_id)
    return int((cursor.page_state or {}).get("offset_id") or 0) if cursor else 0


def advance_source_page(session, source, *, state, snapshots, observed_at):
    cursor = session.get(ChannelSourcePageCursor, state.id)
    if cursor is None:
        cursor = ChannelSourcePageCursor(listener_source_state_id=state.id, page_state={})
        session.add(cursor)
    saved = dict(cursor.page_state or {})
    page = saved if "head_id" in saved else _new_page(session, source, state, snapshots, observed_at,
        completed=saved.get("completed_history", []))
    ids = [int(item.message_id) for item in snapshots]
    offset = int(page.get("offset_id") or 0)
    if offset and ids and max(ids) >= offset:
        raise ValueError("channel_source_page_cursor_not_advanced")
    exhaustive = len(snapshots) < source.fetch_limit
    history = _advance_history(page["history"], snapshots, exhaustive=exhaustive)
    previous = int(page["previous_id"])
    contiguous = not previous or exhaustive or (ids and min(ids) <= previous)
    complete = bool(contiguous and all(row["complete"] for row in history))
    dates = [as_beijing(row.published_at) for row in snapshots if row.published_at]
    oldest = datetime(1970, 1, 1) if exhaustive else min(dates, default=as_beijing(observed_at))
    state.last_event_at = min(as_beijing(state.last_event_at), oldest) if state.last_event_at else oldest
    proof_at = as_beijing(datetime.fromisoformat(page["head_observed_at"]))
    if complete:
        state.last_remote_message_id = str(page["head_id"])
        state.backfill_until = None
        cursor.page_state = {"completed_history": history} if history else {}
    else:
        state.backfill_until = state.backfill_until or state.observed_at or observed_at
        cursor.page_state = {**page, "offset_id": min(ids), "history": history}
    return SourcePageProgress(complete, proof_at)


def _new_page(session, source, state, snapshots, observed_at, *, completed):
    previous = int(state.last_remote_message_id or 0)
    return {"previous_id": previous, "offset_id": 0,
        "head_id": max([int(row.message_id) for row in snapshots] + [previous]),
        "head_observed_at": as_beijing(observed_at).isoformat(),
        "history": _initial_requirements(session, source, completed=completed)}


def _initial_requirements(session, source, *, completed):
    result = []
    tasks = session.scalars(select(Task).where(Task.id.in_(source.task_ids), Task.tenant_id == source.tenant_id))
    for task in tasks:
        identity = history_requirement(task)
        if identity is None or history_initialized(session, task, source.channel_target_id):
            continue
        proven = next((row for row in completed if completed_history_matches(row, identity)), None)
        result.append(proven or {**identity, "keys": [], "complete": False})
    return result


def _advance_history(requirements, snapshots, *, exhaustive):
    result = []
    for row in requirements:
        if row["complete"]:
            result.append(row)
            continue
        anchor = as_beijing(datetime.fromisoformat(row["anchor"]))
        historical = [item for item in snapshots if item.published_at and as_beijing(item.published_at) <= anchor]
        keys = list(dict.fromkeys(row["keys"] + [logical_source_key(item) for item in historical
            if not source_filter_reason(item, task_type=row["task_type"])]))
        wanted = int(row["limit"])
        enough = len(keys) >= wanted and bool(historical)
        boundary = logical_source_key(snapshots[-1]) if snapshots else ""
        partial_album = wanted > 0 and len(keys) >= wanted and keys[wanted-1].startswith("album:") and boundary == keys[wanted-1]
        complete = row["complete"] or exhaustive or (enough and not partial_album)
        result.append({**row, "keys": keys[:wanted], "complete": bool(complete)})
    return result

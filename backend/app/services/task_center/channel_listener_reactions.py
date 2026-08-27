from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

from app.integrations.telegram import ChannelReactionCapabilitySnapshot
from app.models import Task


def probe_reaction_capability(
    fetcher: Callable[..., ChannelReactionCapabilitySnapshot],
    *,
    required: bool,
    account_id: int,
    channel_peer: Any,
    session_ciphertext: Any,
    credentials: Any,
) -> tuple[ChannelReactionCapabilitySnapshot | None, str]:
    if not required:
        return None, ""
    try:
        capability = fetcher(
            account_id,
            channel_peer,
            session_ciphertext,
            credentials,
        )
    except Exception as exc:  # noqa: BLE001 - the typed blocker is persisted below.
        return ChannelReactionCapabilitySnapshot(), type(exc).__name__
    return capability, ""


def record_reaction_probe_state(
    session: Session,
    *,
    task_ids: list[str],
    required: bool,
    error_code: str,
) -> None:
    if not required:
        return
    for task_id in task_ids:
        task = session.get(Task, task_id)
        if task is not None and task.type == "channel_like":
            _set_reaction_probe_state(task, error_code=error_code)


def credential_task(
    session: Session,
    *,
    task_ids: list[str],
    reaction_capability_required: bool,
) -> Task | None:
    tasks = [session.get(Task, task_id) for task_id in task_ids]
    if reaction_capability_required:
        like_task = next((task for task in tasks if task and task.type == "channel_like"), None)
        if like_task is not None:
            return like_task
    return next((task for task in tasks if task is not None), None)


def _set_reaction_probe_state(task: Task, *, error_code: str) -> None:
    stats = dict(task.stats or {})
    if error_code:
        stats["reaction_capability_probe"] = {
            "reason_code": "reaction_capability_probe_failed",
            "error_code": error_code,
        }
        task.last_error = f"Reaction 能力探测失败：{error_code}"
    else:
        stats.pop("reaction_capability_probe", None)
        if task.last_error.startswith("Reaction 能力探测失败"):
            task.last_error = ""
    task.stats = stats

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Action, GroupContextMessage, Task

from .account_voice_profiles import group_stance_summaries, voice_profile_prompt_details
from .payloads import SendMessagePayload


HISTORY_MAX_CHARS = 1000


def rebuild_group_prompt_inputs(
    session: Session,
    task: Task,
    batch: list[tuple[Action, SendMessagePayload]],
) -> list[tuple[Action, SendMessagePayload]]:
    if not batch:
        return batch
    from .executors.group_ai_chat import (
        _account_prompt_profiles,
        _recent_account_memories,
        account_profile_summaries,
    )

    account_ids = [int(action.account_id) for action, _payload in batch if action.account_id]
    group_id = int(batch[0][1].group_id)
    memories = _recent_account_memories(
        session,
        task,
        account_ids,
        group_id=group_id,
        depth=int((task.type_config or {}).get("account_memory_depth") or 3),
    )
    profiles = account_profile_summaries(session, task, account_ids, group_id=group_id)
    voices = voice_profile_prompt_details(
        session,
        tenant_id=task.tenant_id,
        account_ids=account_ids,
    )
    stances = group_stance_summaries(
        session,
        tenant_id=task.tenant_id,
        group_id=group_id,
        account_ids=account_ids,
    )
    prompt_profiles = _account_prompt_profiles(profiles, voices, stances)
    return _apply_prompt_inputs(
        session,
        task,
        batch,
        memories=memories,
        profiles=prompt_profiles,
        stances=stances,
        voices=voices,
    )


def _apply_prompt_inputs(
    session: Session,
    task: Task,
    batch: list[tuple[Action, SendMessagePayload]],
    *,
    memories: dict[str, str],
    profiles: dict[str, str],
    stances: dict[int, str],
    voices: dict[int, dict],
) -> list[tuple[Action, SendMessagePayload]]:
    refreshed = []
    for action, payload in batch:
        account_id = int(action.account_id or 0)
        updated = payload.model_copy(update={
            "ai_generation_history": _scoped_history(session, task, payload),
            "account_memory": memories.get(str(account_id), ""),
            "account_profile": profiles.get(str(account_id), ""),
            "stance_summary": stances.get(account_id, ""),
            **_voice_payload(voices.get(account_id) or {}),
        })
        action.payload = updated.model_dump(mode="json")
        refreshed.append((action, updated))
    return refreshed


def _voice_payload(voice: dict) -> dict:
    version = int(voice.get("version") or 0)
    mask_id = str(voice.get("id") or "")
    summary = str(voice.get("summary") or "")
    active = bool(mask_id and version > 0 and voice.get("snapshot_hash"))
    return {
        "account_voice_profile_version": version,
        "account_voice_profile_summary": summary,
        "account_mask_version": version,
        "account_mask_id": mask_id,
        "account_mask_snapshot_hash": str(voice.get("snapshot_hash") or ""),
        "account_mask_summary": summary,
        "voice_profile_contract_version": str(voice.get("contract_version") or ""),
        "mask_status": "active" if active else "missing",
        "content_source": "account_mask" if active else "",
    }


def _scoped_history(session: Session, task: Task, payload: SendMessagePayload) -> str:
    context_ids = [int(value) for value in payload.context_message_ids if int(value) > 0]
    if not context_ids:
        return ""
    rows = session.scalars(
        select(GroupContextMessage)
        .where(
            GroupContextMessage.tenant_id == task.tenant_id,
            GroupContextMessage.group_id == payload.group_id,
            GroupContextMessage.id.in_(context_ids),
            GroupContextMessage.is_bot.is_(False),
            GroupContextMessage.content != "",
        )
        .order_by(func.coalesce(GroupContextMessage.sent_at, GroupContextMessage.created_at))
    )
    history = "\n".join(f"{row.sender_name}: {row.content}" for row in rows)
    return history[-HISTORY_MAX_CHARS:]

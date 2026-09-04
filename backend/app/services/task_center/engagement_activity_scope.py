from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models import Action, TgGroup


@dataclass(frozen=True)
class ActivityScope:
    canonical_peer_id: str
    canonical_source_identity: str


def action_activity_scope(session: Session, action: Action) -> ActivityScope:
    payload = dict(action.payload or {})
    direct_peer = str(
        payload.get("actual_target_peer")
        or payload.get("discussion_peer_id")
        or payload.get("channel_id")
        or ""
    )
    if not direct_peer:
        group_id = int(payload.get("group_id") or 0)
        group = session.get(TgGroup, group_id) if group_id else None
        direct_peer = str(group.tg_peer_id) if group else ""
    return ActivityScope(
        canonical_peer_id=direct_peer,
        canonical_source_identity=payload_activity_source_identity(payload),
    )


def payload_activity_source_identity(payload: dict) -> str:
    thread_root = str(
        payload.get("source_top_message_id")
        or payload.get("thread_root_message_id")
        or ""
    ).strip()
    if thread_root:
        return f"thread:{thread_root}"
    revision = str(payload.get("source_revision_id") or "").strip()
    if revision:
        return f"source_revision:{revision}"
    message_id = str(payload.get("channel_message_id") or "").strip()
    return f"message:{message_id}" if message_id else ""


__all__ = [
    "ActivityScope",
    "action_activity_scope",
    "payload_activity_source_identity",
]

from __future__ import annotations

import hashlib

from sqlalchemy.orm import Session

from app.models import TaskGroupBotAdmission, TgAccount

from .task_group_bot_admission_facts import record_fact
from .task_group_bot_admission_surface import fact_hash


OBSERVATION_SECONDS = 30


def record_control_facts(
    session: Session,
    admission: TaskGroupBotAdmission,
    messages: list,
    *,
    end_cursor: int,
) -> int:
    matched = 0
    for message in messages:
        if not _ingest_viewer_prompt(session, admission, message):
            continue
        matched += 1
        record_fact(session, admission, "dynamic_channel_follow", outcome={
            "remote_message_id": str(getattr(message, "remote_message_id", "")),
            "sender_peer_id": str(getattr(message, "sender_peer_id", "")),
            "content_hash": hashlib.sha256(
                str(getattr(message, "content", "")).encode()
            ).hexdigest(),
        })
    if not matched:
        return 0
    identity = dict(admission.surface_identity or {})
    identity["observed_end_cursor"] = str(end_cursor)
    admission.state = "requirements_pending"
    admission.surface_identity = identity
    admission.surface_identity_hash = fact_hash(identity)
    admission.version = int(admission.version or 1) + 1
    return matched


def _ingest_viewer_prompt(
    session: Session,
    admission: TaskGroupBotAdmission,
    message,
) -> bool:
    from .group_bot_admission import (
        ensure_admission_after_join,
        ingest_trusted_bot_prompt,
        is_group_bot_control_prompt,
    )

    account = session.get(TgAccount, admission.account_id)
    content = str(getattr(message, "content", "") or "")
    controls = tuple(getattr(message, "control_buttons", ()) or ())
    display_name = str(account.display_name or "").strip() if account else ""
    if not display_name or display_name.lower() not in content.lower():
        return False
    if not is_group_bot_control_prompt(content, controls):
        return False
    is_admin_bot = bool(
        getattr(message, "is_bot", False)
        and str(getattr(message, "sender_role", "")) in {"admin", "owner"}
    )
    if not is_admin_bot:
        return False
    legacy = ensure_admission_after_join(
        session,
        tenant_id=admission.tenant_id,
        group_id=admission.target_group_id,
        account_id=admission.account_id,
        join_start_cursor=str(
            dict(admission.surface_identity or {}).get("observed_start_cursor") or ""
        ),
        observation_window_seconds=OBSERVATION_SECONDS,
    )
    ingest_trusted_bot_prompt(
        session,
        admission=legacy,
        message_id=str(getattr(message, "remote_message_id", "") or ""),
        text=content,
        bot_peer_id=str(getattr(message, "sender_peer_id", "") or ""),
        is_admin_bot=True,
        control_buttons=controls,
        bound_task_id=admission.task_id,
    )
    return True


__all__ = ["record_control_facts"]

from __future__ import annotations

import hashlib

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import Action, TaskGroupBotAdmission

from .task_group_bot_admission_facts import record_fact
from .task_group_bot_admission_prompts import (
    PostSendControlRecovery,
    record_post_send_control_facts,
)


POST_SEND_CONTROL_FETCH_LIMIT = 100


def recover_post_send_interception(
    session: Session,
    action: Action,
    *,
    target_peer: str,
    remote_message_id: str,
    transport,
    fetcher,
) -> PostSendControlRecovery:
    admission = _bound_admission(session, action)
    if admission is None:
        return PostSendControlRecovery("blocked", "task_group_bot_admission_missing")
    try:
        after_message_id = int(str(remote_message_id).strip())
    except (TypeError, ValueError):
        return _block_admission(session, admission, action, "remote_message_id_invalid")
    try:
        messages = fetcher(
            int(action.account_id),
            target_peer,
            transport.session_ciphertext,
            transport.credentials,
            limit=POST_SEND_CONTROL_FETCH_LIMIT,
            control_only=True,
            after_message_id=after_message_id,
        )
    except Exception as exc:  # noqa: BLE001 - transport errors retain the hold for retry.
        return PostSendControlRecovery(
            "retry",
            f"post_send_control_fetch_failed:{type(exc).__name__}",
        )
    result = record_post_send_control_facts(session, admission, list(messages or ()))
    if result.status != "blocked":
        return result
    return _block_admission(session, admission, action, result.reason)


def _bound_admission(
    session: Session,
    action: Action,
) -> TaskGroupBotAdmission | None:
    payload = action.payload if isinstance(action.payload, dict) else {}
    group_id = int(payload.get("group_id") or 0)
    admission_id = str(payload.get("task_group_bot_admission_id") or "")
    statement = select(TaskGroupBotAdmission).where(
        TaskGroupBotAdmission.tenant_id == action.tenant_id,
        TaskGroupBotAdmission.task_id == action.task_id,
        TaskGroupBotAdmission.account_id == action.account_id,
        TaskGroupBotAdmission.target_group_id == group_id,
        TaskGroupBotAdmission.task_lifecycle_epoch == action.task_lifecycle_epoch,
    )
    if admission_id:
        statement = statement.where(TaskGroupBotAdmission.id == admission_id)
    return session.scalar(statement.limit(1))


def _block_admission(
    session: Session,
    admission: TaskGroupBotAdmission,
    action: Action,
    reason: str,
) -> PostSendControlRecovery:
    expected_version = int(admission.version or 1)
    remote_id = str((action.result or {}).get("telegram_msg_id") or "")
    evidence = {
        "outcome": "post_send_intercepted",
        "reason": reason[:120],
        "source_action_id": str(action.id),
        "remote_message_id_hash": hashlib.sha256(remote_id.encode()).hexdigest(),
    }
    changed = session.execute(
        update(TaskGroupBotAdmission)
        .where(
            TaskGroupBotAdmission.id == admission.id,
            TaskGroupBotAdmission.version == expected_version,
        )
        .values(
            state="post_send_intercepted",
            terminal_reason="post_send_intercepted",
            terminal_evidence=evidence,
            version=expected_version + 1,
        )
    ).rowcount
    if changed != 1:
        session.refresh(admission)
        return PostSendControlRecovery("retry", "c2_observation_version_conflict")
    session.refresh(admission)
    record_fact(session, admission, "post_follow_visibility", outcome=evidence)
    return PostSendControlRecovery("blocked", reason)


__all__ = ["POST_SEND_CONTROL_FETCH_LIMIT", "recover_post_send_interception"]

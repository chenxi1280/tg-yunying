from __future__ import annotations

import hashlib

from sqlalchemy.orm import Session

from app.models import Task, TaskGroupBotAdmission, TgAccount
from app.services._common import _now

from .task_group_bot_admission_facts import record_fact
from .task_group_bot_admission_surface import fact_hash
from .group_bot_admission import (
    confirmation_button,
    is_group_bot_control_prompt,
    parse_channel_refs,
    source_channel_url_for_ref,
)
from .payloads import (
    GroupBotConfirmationButtonPayload,
    GroupBotRequiredChannelFollowPayload,
    create_group_bot_confirmation_button_action,
    create_group_bot_required_channel_follow_action,
)


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
            "source_fingerprint": _source_fingerprint(message),
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
    return matched


def _ingest_viewer_prompt(
    session: Session,
    admission: TaskGroupBotAdmission,
    message,
) -> bool:
    account = session.get(TgAccount, admission.account_id)
    task = session.get(Task, admission.task_id)
    content = str(getattr(message, "content", "") or "")
    controls = tuple(getattr(message, "control_buttons", ()) or ())
    display_name = str(account.display_name or "").strip() if account else ""
    message_id = str(getattr(message, "remote_message_id", "") or "")
    if task is None or task.fulfillment_contract_version != "fact_first_v3":
        return False
    if not display_name or display_name.lower() not in content.lower() or not message_id:
        return False
    if not is_group_bot_control_prompt(content, controls):
        return False
    is_admin_bot = bool(
        getattr(message, "is_bot", False)
        and str(getattr(message, "sender_role", "")) in {"admin", "owner"}
    )
    if not is_admin_bot or not str(getattr(message, "sender_peer_id", "") or "").strip():
        return False
    return _materialize_task_requirements(
        session,
        task=task,
        admission=admission,
        message_id=message_id,
        content=content,
        controls=controls,
        bot_peer_id=str(getattr(message, "sender_peer_id", "") or ""),
    )


def _materialize_task_requirements(
    session: Session,
    *,
    task: Task,
    admission: TaskGroupBotAdmission,
    message_id: str,
    content: str,
    controls: tuple,
    bot_peer_id: str,
) -> bool:
    refs = parse_channel_refs(content, controls)
    button = confirmation_button(controls)
    source_fingerprint = _source_fingerprint_values(message_id, bot_peer_id, content, controls)
    identity = dict(admission.surface_identity or {})
    if identity.get("requirement_source_fingerprint") == source_fingerprint:
        return True
    next_version = int(admission.version or 1) + 1
    identity.update({
        "requirement_source_message_id": message_id,
        "requirement_source_fingerprint": source_fingerprint,
        "requirement_bot_peer_id": bot_peer_id,
        "requirement_channel_refs": refs,
    })
    admission.surface_identity = identity
    admission.surface_identity_hash = fact_hash(identity)
    admission.requirement_set_version = int(admission.requirement_set_version or 1) + 1
    admission.version = next_version
    admission.state = "requirements_pending"
    for ref in refs:
        _create_task_follow_action(
            session,
            task=task,
            admission=admission,
            channel_ref=ref,
            source_url=source_channel_url_for_ref(controls, ref, content),
            source_message_id=message_id,
            source_fingerprint=source_fingerprint,
        )
    if button is not None:
        _create_task_confirmation_action(
            session,
            task=task,
            admission=admission,
            source_message_id=message_id,
            source_fingerprint=source_fingerprint,
            bot_peer_id=bot_peer_id,
            button=button,
        )
    session.flush()
    return True


def _create_task_follow_action(
    session: Session,
    *,
    task: Task,
    admission: TaskGroupBotAdmission,
    channel_ref: str,
    source_url: str,
    source_message_id: str,
    source_fingerprint: str,
) -> None:
    if not source_url:
        admission.terminal_evidence = {
            **dict(admission.terminal_evidence or {}),
            "requirement_source_missing": channel_ref,
        }
        return
    payload = GroupBotRequiredChannelFollowPayload(
        group_id=int(admission.target_group_id),
        admission_id=None,
        admission_version=int(admission.version or 1),
        channel_ref=channel_ref,
        source_message_id=source_message_id,
        source_channel_url=source_url,
        admission_bound_task_id=task.id,
        admission_bound_account_id=int(admission.account_id),
        task_group_bot_admission_id=admission.id,
        source_fingerprint=source_fingerprint,
        requirement_action_key=f"{source_fingerprint}:dynamic_channel_follow:{channel_ref.casefold()}",
    )
    create_group_bot_required_channel_follow_action(
        session, task, int(admission.account_id), _now(), payload, flush=True,
    )


def _create_task_confirmation_action(
    session: Session,
    *,
    task: Task,
    admission: TaskGroupBotAdmission,
    source_message_id: str,
    source_fingerprint: str,
    bot_peer_id: str,
    button: dict[str, object],
) -> None:
    payload = GroupBotConfirmationButtonPayload(
        group_id=int(admission.target_group_id),
        admission_id=None,
        admission_version=int(admission.version or 1),
        source_message_id=source_message_id,
        trusted_bot_peer_id=bot_peer_id,
        button_row=int(button["row"]),
        button_col=int(button["col"]),
        button_text=str(button["text"]),
        button_type="callback",
        admission_bound_task_id=task.id,
        admission_bound_account_id=int(admission.account_id),
        task_group_bot_admission_id=admission.id,
        source_fingerprint=source_fingerprint,
        requirement_action_key=f"{source_fingerprint}:requirement_confirmation",
    )
    create_group_bot_confirmation_button_action(
        session, task, int(admission.account_id), _now(), payload, flush=True,
    )


def _source_fingerprint(message) -> str:
    return _source_fingerprint_values(
        str(getattr(message, "remote_message_id", "") or ""),
        str(getattr(message, "sender_peer_id", "") or ""),
        str(getattr(message, "content", "") or ""),
        tuple(getattr(message, "control_buttons", ()) or ()),
    )


def _source_fingerprint_values(
    message_id: str,
    bot_peer_id: str,
    content: str,
    controls: tuple,
) -> str:
    return hashlib.sha256(
        repr((message_id, bot_peer_id, content, controls)).encode()
    ).hexdigest()


__all__ = ["record_control_facts"]

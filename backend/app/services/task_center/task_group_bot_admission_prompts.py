from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass, field

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import Task, TaskGroupBotAdmission, TgAccount
from app.services._common import _now

from .task_group_bot_admission_facts import record_fact
from .task_group_bot_admission_surface import fact_hash
from .group_bot_admission import (
    attribute_prompt_to_account,
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


@dataclass(frozen=True)
class PostSendControlRecovery:
    status: str
    reason: str
    source_message_id: str = ""
    diagnostics: dict = field(default_factory=dict)


def record_post_send_control_facts(
    session: Session,
    admission: TaskGroupBotAdmission,
    messages: list,
) -> PostSendControlRecovery:
    candidates = _task_group_candidate_accounts(session, admission)
    last_reason = "post_send_control_missing"
    reasons = Counter()
    role_errors = Counter()
    for message in messages:
        reason = _strict_post_send_rejection(admission, message, candidates)
        if reason:
            last_reason = reason
            reasons[reason] += 1
            if getattr(message, "sender_role_error", ""):
                role_errors[message.sender_role_error] += 1
            continue
        message_id = str(getattr(message, "remote_message_id", "") or "")
        content = str(getattr(message, "content", "") or "")
        controls = tuple(getattr(message, "control_buttons", ()) or ())
        bot_peer_id = str(getattr(message, "sender_peer_id", "") or "")
        task = session.get(Task, admission.task_id)
        if task is None or not _materialize_task_requirements(
            session,
            task=task,
            admission=admission,
            message_id=message_id,
            content=content,
            controls=controls,
            bot_peer_id=bot_peer_id,
        ):
            return PostSendControlRecovery("retry", "c2_observation_version_conflict")
        _record_post_send_control_fact(session, admission, message)
        return PostSendControlRecovery("matched", "strict_same_view_prompt", message_id,
            diagnostics={"rejection_counts": dict(reasons), "source_role_error_counts": dict(role_errors)})
    return PostSendControlRecovery("blocked", last_reason,
        diagnostics={"rejection_counts": dict(reasons), "source_role_error_counts": dict(role_errors)})


def _task_group_candidate_accounts(
    session: Session,
    admission: TaskGroupBotAdmission,
) -> list[TgAccount]:
    return list(session.scalars(
        select(TgAccount)
        .join(TaskGroupBotAdmission, TaskGroupBotAdmission.account_id == TgAccount.id)
        .where(
            TaskGroupBotAdmission.tenant_id == admission.tenant_id,
            TaskGroupBotAdmission.task_id == admission.task_id,
            TaskGroupBotAdmission.target_group_id == admission.target_group_id,
            TaskGroupBotAdmission.state != "abandoned",
        )
        .distinct()
    ))


def _strict_post_send_rejection(
    admission: TaskGroupBotAdmission,
    message,
    candidates: list[TgAccount],
) -> str:
    message_id = str(getattr(message, "remote_message_id", "") or "").strip()
    bot_peer_id = str(getattr(message, "sender_peer_id", "") or "").strip()
    if not message_id:
        return "post_send_control_message_id_missing"
    sender_role = str(getattr(message, "sender_role", ""))
    if not getattr(message, "is_bot", False):
        return "post_send_control_source_untrusted_non_bot"
    if getattr(message, "sender_role_error", ""):
        return "post_send_control_source_role_lookup_failed"
    if sender_role == "unknown":
        return "post_send_control_source_role_unknown"
    if sender_role not in {"admin", "owner"}:
        return "post_send_control_source_untrusted_non_admin"
    if not bot_peer_id or not _expected_bot_matches(admission, bot_peer_id):
        return "post_send_control_bot_mismatch"
    content = str(getattr(message, "content", "") or "")
    controls = tuple(getattr(message, "control_buttons", ()) or ())
    if not is_group_bot_control_prompt(content, controls):
        return "post_send_control_semantics_missing"
    if confirmation_button(controls) is None:
        return "post_send_control_callback_missing"
    refs = parse_channel_refs(content, controls)
    if not refs or any(not source_channel_url_for_ref(controls, ref, content) for ref in refs):
        return "post_send_control_channel_url_missing"
    return _recipient_rejection(admission, message, candidates)


def _expected_bot_matches(admission: TaskGroupBotAdmission, bot_peer_id: str) -> bool:
    identity = admission.surface_identity if isinstance(admission.surface_identity, dict) else {}
    expected = str(identity.get("requirement_bot_peer_id") or "").strip()
    return not expected or expected == bot_peer_id


def _recipient_rejection(
    admission: TaskGroupBotAdmission,
    message,
    candidates: list[TgAccount],
) -> str:
    account_id, attribution = attribute_prompt_to_account(
        text=str(getattr(message, "content", "") or ""),
        waiting_account_ids=[int(item.id) for item in candidates],
        account_usernames={int(item.id): str(item.username or "") for item in candidates},
        account_display_names={int(item.id): str(item.display_name or "") for item in candidates},
        account_peer_ids={
            int(admission.account_id): str(getattr(message, "viewer_peer_id", "") or "")
        },
    )
    if account_id != int(admission.account_id):
        return f"post_send_control_{attribution}"
    if attribution not in {"explicit_recipient_match", "text_match"}:
        return f"post_send_control_{attribution}"
    return ""


def _record_post_send_control_fact(
    session: Session,
    admission: TaskGroupBotAdmission,
    message,
) -> None:
    record_fact(session, admission, "dynamic_channel_follow", outcome={
        "recovery_source": "post_send_same_view",
        "remote_message_id": str(getattr(message, "remote_message_id", "") or ""),
        "sender_peer_id": str(getattr(message, "sender_peer_id", "") or ""),
        "source_fingerprint": source_fingerprint(message),
        "content_hash": hashlib.sha256(
            str(getattr(message, "content", "") or "").encode()
        ).hexdigest(),
    })


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
            "source_fingerprint": source_fingerprint(message),
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
    source_fingerprint_value = _source_fingerprint_values(message_id, bot_peer_id, content, controls)
    identity = dict(admission.surface_identity or {})
    if identity.get("requirement_source_fingerprint") == source_fingerprint_value:
        return True
    expected_version = int(admission.version or 1)
    identity.update({
        "requirement_source_message_id": message_id,
        "requirement_source_fingerprint": source_fingerprint_value,
        "requirement_bot_peer_id": bot_peer_id,
        "requirement_channel_refs": refs,
    })
    if not _cas_requirement_source(session, admission, expected_version=expected_version, identity=identity):
        current_identity = admission.surface_identity if isinstance(admission.surface_identity, dict) else {}
        return current_identity.get("requirement_source_fingerprint") == source_fingerprint_value
    for ref in refs:
        _create_task_follow_action(
            session,
            task=task,
            admission=admission,
            channel_ref=ref,
            source_url=source_channel_url_for_ref(controls, ref, content),
            source_message_id=message_id,
            source_fingerprint=source_fingerprint_value,
        )
    if button is not None:
        _create_task_confirmation_action(
            session,
            task=task,
            admission=admission,
            source_message_id=message_id,
            source_fingerprint=source_fingerprint_value,
            bot_peer_id=bot_peer_id,
            button=button,
        )
    session.flush()
    return True


def _cas_requirement_source(
    session: Session,
    admission: TaskGroupBotAdmission,
    *,
    expected_version: int,
    identity: dict,
) -> bool:
    updated_id = session.scalar(
        update(TaskGroupBotAdmission)
        .where(
            TaskGroupBotAdmission.id == admission.id,
            TaskGroupBotAdmission.version == expected_version,
        )
        .values(
            surface_identity=identity,
            surface_identity_hash=fact_hash(identity),
            requirement_set_version=int(admission.requirement_set_version or 1) + 1,
            version=expected_version + 1,
            state="requirements_pending",
        )
        .returning(TaskGroupBotAdmission.id)
    )
    session.refresh(admission)
    return updated_id is not None


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


def source_fingerprint(message) -> str:
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


__all__ = [
    "PostSendControlRecovery",
    "record_control_facts",
    "record_post_send_control_facts",
    "source_fingerprint",
]

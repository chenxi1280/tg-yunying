"""Group-bot admission state machine for AI group chat."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    GroupBotAdmission,
    GroupBotAdmissionPolicy,
    GroupBotRequiredChannelFollow,
    PendingVisibilityCredit,
)
from app.models.enums import now as model_now

from .group_bot_observation import has_valid_observation, numeric_cursor, record_observation_batch


DEFAULT_OBSERVATION_WINDOW_SECONDS = 120
DEFAULT_VISIBILITY_WINDOW_SECONDS = 90
READY_STATE = "group_bot_admission_ready"
WAITING_STATES = {
    "awaiting_group_bot_rule",
    "observation_open",
    "group_bot_policy_unresolved",
    "required_channel_follow_pending",
    "following_required_channel",
    "awaiting_group_bot_confirmation",
    "group_bot_rule_unattributed",
    "blocked",
    "observation_stale",
    "post_send_intercepted",
    "abandoned",
    "legacy_group_bot_review",
}

CONFIRMATION_TEMPLATES = (
    "验证通过",
    "可以发言",
    "已解除禁言",
    "已通过验证",
    "验证成功",
    "已放行",
)
SOURCE_BOUND_POLICY_TYPES = {"follow_sufficient", "explicit_bot_confirmation"}
CONFIRMATION_BUTTON_MARKERS = ("我已加入", "我已关注", "已关注", "完成验证", "完成关注", "确认")
PUBLIC_CHANNEL_URL_RE = re.compile(r"^https?://t\.me/([A-Za-z][A-Za-z0-9_]{3,})/?$", re.I)
PUBLIC_CHANNEL_URL_IN_TEXT_RE = re.compile(r"https?://t\.me/([A-Za-z][A-Za-z0-9_]{3,})/?", re.I)
CONTROL_PROMPT_MAX_GAP = 12
CONTROL_PROMPT_PATTERNS = (
    re.compile(rf"(?:请|需|需要|先).{{0,{CONTROL_PROMPT_MAX_GAP}}}(?:关注|订阅|加入|入群)", re.I),
    re.compile(rf"(?:关注|订阅|加入|入群).{{0,{CONTROL_PROMPT_MAX_GAP}}}(?:频道|群|channel|group)", re.I),
    re.compile(rf"(?:频道|群|channel|group).{{0,{CONTROL_PROMPT_MAX_GAP}}}(?:关注|订阅|加入|入群)", re.I),
    re.compile(r"(?:完成验证|验证后|解除禁言|可以发言|发言权限)", re.I),
)
CONTROL_PROMPT_UNVERIFIED_CODE = "group_bot_control_prompt_unverified"
REARMABLE_FOLLOW_STATES = {"awaiting_group_bot_rule", "observation_open"}


@dataclass(frozen=True)
class AdmissionGateDecision:
    allowed: bool
    code: str = ""
    admission_id: int | None = None
    admission_version: int | None = None
    state: str = ""


def is_group_bot_admission_ready(admission: GroupBotAdmission | None, *, canary_enforce: bool = True) -> bool:
    if not canary_enforce:
        return True
    if admission is None:
        # C1 legacy accounts may not have rows yet.
        return True
    return admission.state == READY_STATE and admission.state != "abandoned"


def get_admission(
    session: Session,
    *,
    tenant_id: int,
    group_id: int,
    account_id: int,
) -> GroupBotAdmission | None:
    return session.scalar(
        select(GroupBotAdmission).where(
            GroupBotAdmission.tenant_id == tenant_id,
            GroupBotAdmission.group_id == group_id,
            GroupBotAdmission.account_id == account_id,
        )
    )


def ensure_admission_after_join(
    session: Session,
    *,
    tenant_id: int,
    group_id: int,
    account_id: int,
    membership_action_id: str = "",
    join_start_cursor: str = "",
    observation_window_seconds: int = DEFAULT_OBSERVATION_WINDOW_SECONDS,
) -> GroupBotAdmission:
    existing = get_admission(session, tenant_id=tenant_id, group_id=group_id, account_id=account_id)
    now = model_now()
    closes = now + timedelta(seconds=max(60, min(300, int(observation_window_seconds or DEFAULT_OBSERVATION_WINDOW_SECONDS))))
    state, failure_code = _observation_start_state(join_start_cursor)
    if existing is None:
        row = GroupBotAdmission(
            tenant_id=tenant_id,
            group_id=group_id,
            account_id=account_id,
            membership_action_id=str(membership_action_id or ""),
            state=state,
            join_start_cursor=str(join_start_cursor or ""),
            failure_code=failure_code,
            join_success_at=now,
            observation_closes_at=closes,
            admission_version=1,
            transport_observation={},
            required_channel_refs=[],
        )
        session.add(row)
        session.flush()
        return row

    # Rejoin / new membership generation resets observation.
    if membership_action_id and existing.membership_action_id != str(membership_action_id):
        existing.membership_action_id = str(membership_action_id)
        existing.state = state
        existing.admission_version = int(existing.admission_version or 1) + 1
        existing.join_start_cursor = str(join_start_cursor or "")
        existing.observed_end_cursor = ""
        existing.trusted_bot_peer_id = ""
        existing.required_channel_refs = []
        existing.failure_code = failure_code
        existing.join_success_at = now
        existing.observation_closes_at = closes
        existing.post_send_visibility_state = ""
        session.flush()
    return existing


def _observation_start_state(join_start_cursor: str) -> tuple[str, str]:
    if numeric_cursor(join_start_cursor) is not None:
        return "awaiting_group_bot_rule", ""
    return "observation_stale", "join_start_cursor_missing"


def active_policy(
    session: Session,
    *,
    tenant_id: int,
    group_id: int,
    completion_policy: str,
    trusted_bot_peer_id: str = "",
) -> GroupBotAdmissionPolicy | None:
    stmt = select(GroupBotAdmissionPolicy).where(
        GroupBotAdmissionPolicy.tenant_id == tenant_id,
        GroupBotAdmissionPolicy.group_id == group_id,
        GroupBotAdmissionPolicy.completion_policy == completion_policy,
        GroupBotAdmissionPolicy.status == "active",
    )
    if completion_policy in SOURCE_BOUND_POLICY_TYPES:
        stmt = stmt.where(GroupBotAdmissionPolicy.trusted_bot_peer_id == str(trusted_bot_peer_id or ""))
    return session.scalar(stmt.order_by(GroupBotAdmissionPolicy.policy_version.desc()).limit(1))


def create_policy(
    session: Session,
    *,
    tenant_id: int,
    group_id: int,
    completion_policy: str,
    reason: str,
    evidence_ref: str,
    created_by: str,
    trusted_bot_peer_id: str = "",
    expected_policy_version: int | None = None,
) -> GroupBotAdmissionPolicy:
    if completion_policy not in {"not_required", "follow_sufficient", "explicit_bot_confirmation"}:
        raise ValueError("unsupported completion_policy")
    if completion_policy in SOURCE_BOUND_POLICY_TYPES and not str(trusted_bot_peer_id or "").strip():
        raise ValueError(f"{completion_policy} requires trusted_bot_peer_id")
    # Active uniqueness: one not_required per group; one follow_sufficient per group+bot.
    current = active_policy(
        session,
        tenant_id=tenant_id,
        group_id=group_id,
        completion_policy=completion_policy,
        trusted_bot_peer_id=trusted_bot_peer_id,
    )
    current_version = int(current.policy_version or 1) if current else 0
    if expected_policy_version is not None and int(expected_policy_version) != current_version:
        raise ValueError("policy_version_conflict")
    if current is not None:
        current.status = "revoked"
        current.revoked_by = created_by
        current.revoked_at = model_now()
    row = GroupBotAdmissionPolicy(
        tenant_id=tenant_id,
        group_id=group_id,
        trusted_bot_peer_id=str(trusted_bot_peer_id or ""),
        completion_policy=completion_policy,
        evidence_ref=str(evidence_ref or ""),
        reason=str(reason or ""),
        policy_version=current_version + 1,
        status="active",
        created_by=created_by,
        effective_at=model_now(),
    )
    session.add(row)
    session.flush()
    return row


def is_trusted_group_bot_source(
    session: Session,
    *,
    tenant_id: int,
    group_id: int,
    bot_peer_id: str,
    is_admin_bot: bool,
) -> bool:
    peer = str(bot_peer_id or "").strip()
    if not peer:
        return False
    if is_admin_bot:
        return True
    if session.scalar(
        select(GroupBotAdmission.id).where(
            GroupBotAdmission.tenant_id == tenant_id,
            GroupBotAdmission.group_id == group_id,
            GroupBotAdmission.trusted_bot_peer_id == peer,
        ).limit(1)
    ):
        return True
    return any(
        active_policy(
            session,
            tenant_id=tenant_id,
            group_id=group_id,
            completion_policy=policy_type,
            trusted_bot_peer_id=peer,
        )
        is not None
        for policy_type in SOURCE_BOUND_POLICY_TYPES
    )


def abandon_admission(
    session: Session,
    *,
    admission: GroupBotAdmission,
    reason: str,
    evidence_ref: str,
    abandoned_by: str,
    expected_admission_version: int,
) -> GroupBotAdmission:
    if int(admission.admission_version or 1) != int(expected_admission_version):
        raise ValueError("admission_version_conflict")
    admission.state = "abandoned"
    admission.abandoned_reason = str(reason or "")
    admission.evidence_ref = str(evidence_ref or admission.evidence_ref or "")
    admission.abandoned_by = str(abandoned_by or "")
    admission.abandoned_at = model_now()
    admission.failure_code = "admission_abandoned"
    session.flush()
    return admission


def close_observation_if_due(
    session: Session,
    *,
    admission: GroupBotAdmission,
    now: datetime | None = None,
) -> GroupBotAdmission:
    if admission.state not in {"awaiting_group_bot_rule", "observation_open", "observation_stale"}:
        return admission
    if admission.state == "observation_stale":
        return admission
    current = now or model_now()
    closes_at = admission.observation_closes_at
    if closes_at is not None and current < closes_at:
        if has_valid_observation(session, admission=admission):
            admission.state = "observation_open"
            session.flush()
        return admission
    if not has_valid_observation(session, admission=admission):
        admission.state = "observation_stale"
        admission.failure_code = _observation_evidence_failure(admission)
        session.flush()
        return admission
    # Observation window closed with continuous cursor and no trusted rule.
    policy = active_policy(
        session,
        tenant_id=admission.tenant_id,
        group_id=admission.group_id,
        completion_policy="not_required",
    )
    if policy is not None:
        admission.state = "group_bot_rule_clear"
        admission.completion_policy = "not_required"
        admission.policy_version = int(policy.policy_version or 1)
        # Ready only after can_send is independently true; mark admission ready for gate.
        admission.state = READY_STATE
        admission.failure_code = ""
    else:
        admission.state = "group_bot_policy_unresolved"
        admission.failure_code = "group_bot_policy_unresolved"
    session.flush()
    return admission


def _observation_evidence_failure(admission: GroupBotAdmission) -> str:
    return "join_start_cursor_missing" if numeric_cursor(admission.join_start_cursor) is None else "observation_evidence_missing"


def parse_channel_refs(text: str, control_buttons: tuple[object, ...] | list[object] = ()) -> list[str]:
    refs = _text_channel_refs(text)
    refs.extend(_button_channel_refs(control_buttons))
    return _unique_channel_refs(refs)


def _text_channel_refs(text: str) -> list[str]:
    refs: list[str] = []
    for match in PUBLIC_CHANNEL_URL_IN_TEXT_RE.finditer(text or ""):
        username = match.group(1)
        if username.lower() in {"joinchat", "addstickers"} or username.lower().endswith("bot"):
            continue
        refs.append(username)
    return refs


def _button_channel_refs(control_buttons: tuple[object, ...] | list[object]) -> list[str]:
    refs: list[str] = []
    for button in control_buttons:
        if _button_field(button, "action_type") != "url":
            continue
        match = PUBLIC_CHANNEL_URL_RE.match(_button_field(button, "url"))
        if match and not match.group(1).lower().endswith("bot"):
            refs.append(match.group(1))
    return refs


def _unique_channel_refs(refs: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in refs:
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _button_field(button: object, field: str) -> str:
    value = button.get(field, "") if isinstance(button, dict) else getattr(button, field, "")
    return str("" if value is None else value).strip()


def confirmation_button(control_buttons: tuple[object, ...] | list[object]) -> dict[str, object] | None:
    for button in control_buttons:
        text = _button_field(button, "text")
        if _button_field(button, "action_type") != "callback" or not text:
            continue
        if any(marker in text for marker in CONFIRMATION_BUTTON_MARKERS):
            return {
                "row": int(_button_field(button, "row") or 0),
                "col": int(_button_field(button, "col") or 0),
                "text": text,
                "action_type": "callback",
            }
    return None


def is_group_bot_control_prompt(
    text: str,
    control_buttons: tuple[object, ...] | list[object] = (),
) -> bool:
    if confirmation_button(control_buttons) is not None:
        return True
    if not parse_channel_refs(text, control_buttons):
        return False
    return any(pattern.search(text or "") for pattern in CONTROL_PROMPT_PATTERNS)


def is_group_bot_completion_event(text: str, *, button_confirmed: bool = False) -> bool:
    return bool(button_confirmed) or any(token in (text or "") for token in CONFIRMATION_TEMPLATES)


def source_channel_url_for_ref(
    control_buttons: tuple[object, ...] | list[object],
    channel_ref: str,
    prompt_text: str = "",
) -> str:
    for button in control_buttons:
        url = _button_field(button, "url")
        match = PUBLIC_CHANNEL_URL_RE.match(url)
        if _button_field(button, "action_type") == "url" and match and match.group(1).lower() == channel_ref.lower():
            return url
    for match in PUBLIC_CHANNEL_URL_IN_TEXT_RE.finditer(prompt_text or ""):
        if match.group(1).lower() == channel_ref.lower():
            return match.group(0)
    return ""


def attribute_prompt_to_account(
    *,
    text: str,
    waiting_account_ids: list[int],
    account_usernames: dict[int, str],
    account_display_names: dict[int, str],
    explicit_account_id: int | None = None,
) -> tuple[int | None, str]:
    if explicit_account_id and explicit_account_id in waiting_account_ids:
        return int(explicit_account_id), "explicit"
    text_l = (text or "").lower()
    hits: list[int] = []
    for account_id in waiting_account_ids:
        username = str(account_usernames.get(account_id) or "").strip().lstrip("@").lower()
        display = str(account_display_names.get(account_id) or "").strip().lower()
        if username and (f"@{username}" in text_l or username in text_l):
            hits.append(account_id)
            continue
        if display and display in text_l:
            hits.append(account_id)
    hits = list(dict.fromkeys(hits))
    if len(hits) == 1:
        return hits[0], "text_match"
    if len(waiting_account_ids) == 1:
        return int(waiting_account_ids[0]), "unique_waiting"
    return None, "unattributed"


def ingest_trusted_bot_prompt(
    session: Session,
    *,
    admission: GroupBotAdmission,
    message_id: str,
    text: str,
    bot_peer_id: str,
    is_admin_bot: bool,
    is_trusted_source: bool = False,
    control_buttons: tuple[object, ...] | list[object] = (),
    bound_task_id: str = "",
) -> GroupBotAdmission:
    if not (is_admin_bot or is_trusted_source):
        return admission
    if not is_group_bot_control_prompt(text, control_buttons):
        return admission
    peer = str(bot_peer_id or "").strip()
    if not _record_trusted_prompt(admission, peer, message_id):
        session.flush()
        return admission
    refs = parse_channel_refs(text, control_buttons)
    admission.required_channel_refs = refs
    _record_required_channel_rows(session, admission, refs, message_id)
    admission.state = "required_channel_follow_pending" if refs else "awaiting_group_bot_confirmation"
    admission.completion_policy = admission.completion_policy or "explicit_bot_confirmation"
    session.flush()
    if bound_task_id:
        plan_required_channel_follow_actions(
            session,
            admission=admission,
            task_id=str(bound_task_id),
            source_message_id=str(message_id or ""),
            control_buttons=control_buttons,
            prompt_text=text,
        )
        plan_confirmation_button_action(
            session,
            admission=admission,
            task_id=str(bound_task_id),
            source_message_id=str(message_id or ""),
            control_buttons=control_buttons,
        )
    return admission


def _record_trusted_prompt(admission: GroupBotAdmission, peer: str, message_id: str) -> bool:
    if not peer:
        return False
    if admission.trusted_bot_peer_id and admission.trusted_bot_peer_id != peer:
        admission.state = "blocked"
        admission.failure_code = "group_bot_multi_bot_conflict"
        return False
    admission.trusted_bot_peer_id = peer
    admission.source_message_id = str(message_id or "")
    return True


def _record_required_channel_rows(
    session: Session,
    admission: GroupBotAdmission,
    refs: list[str],
    message_id: str,
) -> None:
    for ref in refs:
        existing = session.scalar(
            select(GroupBotRequiredChannelFollow).where(
                GroupBotRequiredChannelFollow.admission_id == admission.id,
                GroupBotRequiredChannelFollow.channel_ref == ref,
            )
        )
        if existing is None:
            session.add(
                GroupBotRequiredChannelFollow(
                    admission_id=admission.id,
                    channel_ref=ref,
                    source_message_id=str(message_id or ""),
                    status="pending",
                )
            )
            continue
        _rearm_follow_after_verified_restart(existing, admission, message_id)


def _rearm_follow_after_verified_restart(
    follow: GroupBotRequiredChannelFollow,
    admission: GroupBotAdmission,
    message_id: str,
) -> None:
    new_source = str(message_id or "")
    if (
        follow.status != "blocked"
        or follow.failure_code != CONTROL_PROMPT_UNVERIFIED_CODE
        or admission.state not in REARMABLE_FOLLOW_STATES
        or not new_source
        or follow.source_message_id == new_source
    ):
        return
    follow.source_message_id = new_source
    follow.action_id = ""
    follow.resolved_peer_id = ""
    follow.resolved_type = ""
    follow.status = "pending"
    follow.failure_code = ""
    follow.completed_at = None


def current_required_channel_refs(admission: GroupBotAdmission) -> tuple[str, ...]:
    return tuple(str(ref) for ref in (admission.required_channel_refs or []) if str(ref).strip())


def has_pending_required_channel_follows(session: Session, *, admission: GroupBotAdmission) -> bool:
    refs = current_required_channel_refs(admission)
    if not refs:
        return False
    return session.scalar(
        select(GroupBotRequiredChannelFollow.id)
        .where(
            GroupBotRequiredChannelFollow.admission_id == admission.id,
            GroupBotRequiredChannelFollow.channel_ref.in_(refs),
            GroupBotRequiredChannelFollow.status != "success",
        )
        .limit(1)
    ) is not None


def plan_required_channel_follow_actions(
    session: Session,
    *,
    admission: GroupBotAdmission,
    task_id: str,
    source_message_id: str = "",
    control_buttons: tuple[object, ...] | list[object] = (),
    prompt_text: str = "",
) -> list[Any]:
    """Create bound group_bot_channel_follow Actions for pending refs."""
    from app.models import Task
    from app.services._common import _now
    from app.services.task_center.payloads import (
        GroupBotRequiredChannelFollowPayload,
        create_group_bot_required_channel_follow_action,
    )

    task = session.get(Task, task_id)
    if task is None or task.tenant_id != admission.tenant_id:
        return []
    refs = current_required_channel_refs(admission)
    if not refs:
        return []
    pending_refs = session.scalars(
        select(GroupBotRequiredChannelFollow).where(
            GroupBotRequiredChannelFollow.admission_id == admission.id,
            GroupBotRequiredChannelFollow.channel_ref.in_(refs),
            GroupBotRequiredChannelFollow.status == "pending",
        )
    ).all()
    created: list[Any] = []
    now = _now()
    for row in pending_refs:
        if row.action_id:
            continue
        source_url = source_channel_url_for_ref(control_buttons, str(row.channel_ref), prompt_text)
        if not source_url:
            row.failure_code = "required_channel_source_missing"
            admission.failure_code = "required_channel_source_missing"
            continue
        payload = GroupBotRequiredChannelFollowPayload(
            group_id=int(admission.group_id),
            admission_id=int(admission.id),
            admission_version=int(admission.admission_version or 1),
            channel_ref=str(row.channel_ref),
            source_message_id=str(source_message_id or row.source_message_id or ""),
            source_channel_url=source_url,
            admission_bound_task_id=str(task.id),
            admission_bound_account_id=int(admission.account_id),
        )
        action = create_group_bot_required_channel_follow_action(
            session,
            task,
            int(admission.account_id),
            now,
            payload,
            flush=True,
        )
        row.action_id = str(action.id)
        created.append(action)
    session.flush()
    return created


def plan_confirmation_button_action(
    session: Session,
    *,
    admission: GroupBotAdmission,
    task_id: str,
    source_message_id: str,
    control_buttons: tuple[object, ...] | list[object],
) -> Any | None:
    from app.models import Task
    from app.services._common import _now
    from app.services.task_center.payloads import (
        GroupBotConfirmationButtonPayload,
        create_group_bot_confirmation_button_action,
    )

    button = confirmation_button(control_buttons)
    task = session.get(Task, task_id)
    if button is None or task is None or task.tenant_id != admission.tenant_id:
        return None
    if active_policy(
        session,
        tenant_id=admission.tenant_id,
        group_id=admission.group_id,
        completion_policy="follow_sufficient",
        trusted_bot_peer_id=admission.trusted_bot_peer_id,
    ) is not None:
        return None
    if _has_planned_confirmation_action(session, task.id, admission.id, str(source_message_id)):
        return None
    payload = GroupBotConfirmationButtonPayload(
        group_id=int(admission.group_id),
        admission_id=int(admission.id),
        admission_version=int(admission.admission_version or 1),
        source_message_id=str(source_message_id),
        trusted_bot_peer_id=str(admission.trusted_bot_peer_id),
        button_row=int(button["row"]),
        button_col=int(button["col"]),
        button_text=str(button["text"]),
        button_type="callback",
        admission_bound_task_id=str(task.id),
        admission_bound_account_id=int(admission.account_id),
    )
    return create_group_bot_confirmation_button_action(
        session, task, int(admission.account_id), _now(), payload, flush=True
    )


def _has_planned_confirmation_action(session: Session, task_id: str, admission_id: int, source_message_id: str) -> bool:
    from app.models import Action

    actions = session.scalars(
        select(Action).where(
            Action.task_id == task_id,
            Action.action_type == "group_bot_confirmation_button",
        )
    )
    return any(
        int((action.payload or {}).get("admission_id", 0) or 0) == admission_id
        and str((action.payload or {}).get("source_message_id") or "") == source_message_id
        for action in actions
    )


def resolve_bound_task_id_for_group(session: Session, *, tenant_id: int, group_id: int) -> str:
    """Pick a running group_ai_chat task for this group to bind follow actions."""
    from app.models import Task

    task = session.scalar(
        select(Task)
        .where(
            Task.tenant_id == tenant_id,
            Task.type == "group_ai_chat",
            Task.status.in_(("draft", "pending", "running", "paused")),
            Task.deleted_at.is_(None),
            Task.type_config["target_group_id"].as_integer() == int(group_id),
        )
        .order_by(Task.updated_at.desc(), Task.id.desc())
        .limit(1)
    )
    return str(task.id) if task is not None else ""


def mark_channel_follow_completed(
    session: Session,
    *,
    admission: GroupBotAdmission,
    channel_ref: str,
    resolved_peer_id: str = "",
    resolved_type: str = "broadcast",
    action_id: str = "",
) -> GroupBotAdmission:
    active_refs = current_required_channel_refs(admission)
    if channel_ref not in active_refs:
        raise ValueError("required_channel_ref_not_active")
    row = session.scalar(
        select(GroupBotRequiredChannelFollow).where(
            GroupBotRequiredChannelFollow.admission_id == admission.id,
            GroupBotRequiredChannelFollow.channel_ref == channel_ref,
        )
    )
    if row is None:
        raise ValueError("required_channel_ref_not_found")
    row.status = "success"
    row.resolved_peer_id = str(resolved_peer_id or "")
    row.resolved_type = str(resolved_type or "broadcast")
    row.action_id = str(action_id or row.action_id or "")
    row.completed_at = model_now()
    session.flush()
    if has_pending_required_channel_follows(session, admission=admission):
        admission.state = "following_required_channel"
        session.flush()
        return admission
    follow_policy = active_policy(
        session,
        tenant_id=admission.tenant_id,
        group_id=admission.group_id,
        completion_policy="follow_sufficient",
        trusted_bot_peer_id=admission.trusted_bot_peer_id,
    )
    if follow_policy is not None:
        admission.state = READY_STATE
        admission.completion_policy = "follow_sufficient"
        admission.policy_version = int(follow_policy.policy_version or 1)
        admission.failure_code = ""
    else:
        admission.state = "awaiting_group_bot_confirmation"
        admission.completion_policy = "explicit_bot_confirmation"
    session.flush()
    return admission


def apply_confirmation_event(
    session: Session,
    *,
    admission: GroupBotAdmission,
    message_id: str,
    text: str,
    bot_peer_id: str,
    button_confirmed: bool = False,
) -> GroupBotAdmission:
    if admission.state not in {"awaiting_group_bot_confirmation", "following_required_channel", READY_STATE}:
        return admission
    peer = str(bot_peer_id or "").strip()
    if admission.trusted_bot_peer_id and peer and admission.trusted_bot_peer_id != peer:
        admission.failure_code = "group_bot_multi_bot_conflict"
        admission.state = "blocked"
        session.flush()
        return admission
    if not is_group_bot_completion_event(text, button_confirmed=button_confirmed):
        return admission
    admission.confirmation_message_id = str(message_id or "")
    admission.state = READY_STATE
    admission.completion_policy = admission.completion_policy or "explicit_bot_confirmation"
    admission.failure_code = ""
    session.flush()
    return admission


def record_probe_observation(admission: GroupBotAdmission, probe: dict[str, Any]) -> None:
    """Transport probe only; never promotes ready."""
    observation = dict(admission.transport_observation or {})
    observation["last_probe"] = dict(probe or {})
    observation["updated_at"] = model_now().isoformat()
    admission.transport_observation = observation


def evaluate_send_gate(
    session: Session,
    *,
    tenant_id: int,
    group_id: int,
    account_id: int,
    enforce: bool = True,
) -> AdmissionGateDecision:
    admission = get_admission(session, tenant_id=tenant_id, group_id=group_id, account_id=account_id)
    if not enforce:
        return AdmissionGateDecision(True, code="legacy_send_until_reviewed", state=admission.state if admission else "")
    if admission is None:
        # C1 canary: stock accounts without admission rows keep legacy send path.
        return AdmissionGateDecision(True, code="legacy_send_until_reviewed", state="missing")
    close_observation_if_due(session, admission=admission)
    if admission.state == "abandoned":
        return AdmissionGateDecision(
            False,
            code="admission_abandoned",
            admission_id=admission.id,
            admission_version=int(admission.admission_version or 1),
            state=admission.state,
        )
    if admission.state != READY_STATE:
        return AdmissionGateDecision(
            False,
            code="group_bot_admission_wait",
            admission_id=admission.id,
            admission_version=int(admission.admission_version or 1),
            state=admission.state,
        )
    return AdmissionGateDecision(
        True,
        code="ready",
        admission_id=admission.id,
        admission_version=int(admission.admission_version or 1),
        state=admission.state,
    )


def needs_post_send_visibility(admission: GroupBotAdmission | None, *, action_admission_version: int | None) -> bool:
    if admission is None:
        return False
    if admission.state != READY_STATE:
        return False
    if admission.post_send_visibility_state in {"", "required", "pending"}:
        # First message after ready / version change.
        if action_admission_version is None:
            return True
        return int(action_admission_version) == int(admission.admission_version or 1) and admission.post_send_visibility_state != "visible_confirmed"
    return False


def open_pending_visibility_credit(
    session: Session,
    *,
    tenant_id: int,
    action_id: str,
    remote_message_id: str,
    execution_attempt_id: str | None = None,
    bucket_id: int | None = None,
) -> PendingVisibilityCredit:
    existing = session.scalar(select(PendingVisibilityCredit).where(PendingVisibilityCredit.action_id == action_id))
    if existing is not None:
        return existing
    row = PendingVisibilityCredit(
        tenant_id=tenant_id,
        action_id=action_id,
        bucket_id=bucket_id,
        execution_attempt_id=execution_attempt_id,
        remote_message_id=str(remote_message_id or ""),
        hold_reason="pending_visibility",
        status="open",
    )
    session.add(row)
    session.flush()
    return row


def close_pending_visibility_credit(
    session: Session,
    *,
    action_id: str,
    status: str,
) -> PendingVisibilityCredit | None:
    row = session.scalar(select(PendingVisibilityCredit).where(PendingVisibilityCredit.action_id == action_id))
    if row is None:
        return None
    row.status = status
    row.closed_at = model_now()
    session.flush()
    return row


def mark_post_send_intercepted(session: Session, *, admission: GroupBotAdmission) -> None:
    admission.state = "post_send_intercepted"
    admission.post_send_visibility_state = "post_send_intercepted"
    admission.failure_code = "post_send_intercepted"
    admission.admission_version = int(admission.admission_version or 1) + 1
    session.flush()


def mark_visible_confirmed(session: Session, *, admission: GroupBotAdmission) -> None:
    admission.post_send_visibility_state = "visible_confirmed"
    session.flush()


def reconcile_unresolved_with_not_required(session: Session, *, tenant_id: int, group_id: int) -> int:
    policy = active_policy(
        session,
        tenant_id=tenant_id,
        group_id=group_id,
        completion_policy="not_required",
    )
    if policy is None:
        return 0
    rows = session.scalars(
        select(GroupBotAdmission).where(
            GroupBotAdmission.tenant_id == tenant_id,
            GroupBotAdmission.group_id == group_id,
            GroupBotAdmission.state == "group_bot_policy_unresolved",
        )
    ).all()
    count = 0
    for row in rows:
        row.state = READY_STATE
        row.completion_policy = "not_required"
        row.policy_version = int(policy.policy_version or 1)
        row.failure_code = ""
        count += 1
    session.flush()
    return count


def reopen_admission(
    session: Session,
    *,
    admission: GroupBotAdmission,
    expected_admission_version: int,
    reopened_by: str = "",
) -> GroupBotAdmission:
    """PRD §5.8.2 #6: reopen is the only recovery path from abandoned."""
    if admission.state != "abandoned":
        raise ValueError("admission_not_abandoned")
    if int(admission.admission_version or 1) != int(expected_admission_version):
        raise ValueError("admission_version_conflict")
    admission.join_start_cursor = ""
    admission.observed_end_cursor = ""
    admission.state, admission.failure_code = _observation_start_state(admission.join_start_cursor)
    admission.abandoned_reason = ""
    admission.post_send_visibility_state = ""
    admission.admission_version = int(admission.admission_version or 1) + 1
    admission.observation_closes_at = model_now() + timedelta(seconds=DEFAULT_OBSERVATION_WINDOW_SECONDS)
    session.flush()
    return admission


def revoke_policy(
    session: Session,
    *,
    policy: GroupBotAdmissionPolicy,
    revoked_by: str,
    expected_policy_version: int,
) -> GroupBotAdmissionPolicy:
    """Explicit revoke of an active policy."""
    if int(policy.policy_version or 1) != int(expected_policy_version):
        raise ValueError("policy_version_conflict")
    if policy.status != "active":
        raise ValueError("policy_not_active")
    policy.status = "revoked"
    policy.revoked_by = str(revoked_by or "")
    policy.revoked_at = model_now()
    session.flush()
    return policy


__all__ = [
    "AdmissionGateDecision",
    "DEFAULT_OBSERVATION_WINDOW_SECONDS",
    "DEFAULT_VISIBILITY_WINDOW_SECONDS",
    "READY_STATE",
    "WAITING_STATES",
    "is_group_bot_admission_ready",
    "get_admission",
    "ensure_admission_after_join",
    "active_policy",
    "create_policy",
    "abandon_admission",
    "reopen_admission",
    "revoke_policy",
    "record_observation_batch",
    "close_observation_if_due",
    "parse_channel_refs",
    "is_group_bot_control_prompt",
    "is_group_bot_completion_event",
    "current_required_channel_refs",
    "has_pending_required_channel_follows",
    "attribute_prompt_to_account",
    "ingest_trusted_bot_prompt",
    "plan_required_channel_follow_actions",
    "resolve_bound_task_id_for_group",
    "mark_channel_follow_completed",
    "apply_confirmation_event",
    "record_probe_observation",
    "evaluate_send_gate",
    "needs_post_send_visibility",
    "open_pending_visibility_credit",
    "close_pending_visibility_credit",
    "mark_post_send_intercepted",
    "mark_visible_confirmed",
    "reconcile_unresolved_with_not_required",
]

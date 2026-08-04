"""Group-bot admission state machine for AI group chat."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Action,
    ExecutionAttempt,
    GroupBotAdmission,
    GroupBotAdmissionPolicy,
    GroupBotRequiredChannelFollow,
    PendingVisibilityCredit,
    TaskGroupDailyMessageSlot,
)
from app.models.enums import now as model_now
from app.timezone import as_beijing

from .group_bot_observation import has_valid_observation, numeric_cursor, record_observation_batch


DEFAULT_OBSERVATION_WINDOW_SECONDS = 120
DEFAULT_VISIBILITY_WINDOW_SECONDS = 90
READY_STATE = "group_bot_admission_ready"
LEGACY_VISIBILITY_PROBE_STATES = frozenset(
    {
        "awaiting_group_bot_rule",
        "observation_open",
        "observation_stale",
        "group_bot_policy_unresolved",
        "group_bot_rule_unattributed",
    }
)
WAITING_STATES = {
    "awaiting_group_bot_rule",
    "observation_open",
    "group_bot_policy_unresolved",
    "required_channel_follow_pending",
    "following_required_channel",
    "awaiting_group_bot_confirmation",
    "post_follow_visibility_probe",
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
EXPLICIT_RECIPIENT_PREFIX_RE = re.compile(
    r"^\s*(?P<recipient>[^,，\r\n]{1,80})[,，]\s*(?:您|你)?(?:需要|需|请|先)",
    re.I,
)
CONTROL_PROMPT_UNVERIFIED_CODE = "group_bot_control_prompt_unverified"
REARMABLE_FOLLOW_STATES = {"awaiting_group_bot_rule", "observation_open"}
OPEN_CONFIRMATION_ACTION_STATUSES = frozenset({"pending", "claiming", "executing"})
PRE_GATEWAY_TERMINAL_ACTION_STATUSES = frozenset({"failed", "skipped"})
POST_FOLLOW_PROBE_ACTION_KEY = "post_follow_probe_action_id"
GROUP_BOT_CONFIRMATION_SUPERSEDED_CODE = "group_bot_confirmation_superseded"


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
    current = as_beijing(now or model_now()) or model_now()
    closes_at = as_beijing(admission.observation_closes_at)
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
    account_peer_ids: dict[int, str] | None = None,
    explicit_account_id: int | None = None,
) -> tuple[int | None, str]:
    if explicit_account_id and explicit_account_id in waiting_account_ids:
        return int(explicit_account_id), "explicit"
    text_l = (text or "").lower()
    recipient = _explicit_control_recipient(text)
    match_text = recipient.lower() if recipient else text_l
    hits: list[int] = []
    for account_id in waiting_account_ids:
        peer_id = str((account_peer_ids or {}).get(account_id) or "").strip()
        username = str(account_usernames.get(account_id) or "").strip().lstrip("@").lower()
        display = str(account_display_names.get(account_id) or "").strip().lower()
        if recipient and peer_id and recipient == peer_id:
            hits.append(account_id)
            continue
        if username and (f"@{username}" in match_text or username in match_text):
            hits.append(account_id)
            continue
        if display and display in match_text:
            hits.append(account_id)
    hits = list(dict.fromkeys(hits))
    if len(hits) == 1:
        return hits[0], "explicit_recipient_match" if recipient else "text_match"
    if recipient:
        return None, "explicit_recipient_unmatched" if not hits else "explicit_recipient_ambiguous"
    if len(waiting_account_ids) == 1:
        return int(waiting_account_ids[0]), "unique_waiting"
    return None, "unattributed"


def _explicit_control_recipient(text: str) -> str:
    match = EXPLICIT_RECIPIENT_PREFIX_RE.match(text or "")
    return str(match.group("recipient")).strip() if match else ""


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
    bind_confirmation_source: bool = True,
) -> GroupBotAdmission:
    if not (is_admin_bot or is_trusted_source):
        return admission
    if not is_group_bot_control_prompt(text, control_buttons):
        return admission
    previous_state = admission.state
    peer = str(bot_peer_id or "").strip()
    trusted = (
        _record_trusted_prompt(admission, peer, message_id)
        if bind_confirmation_source
        else _record_trusted_peer(admission, peer)
    )
    if not trusted:
        session.flush()
        return admission
    refs = parse_channel_refs(text, control_buttons)
    admission.required_channel_refs = refs
    _record_required_channel_rows(session, admission, refs, message_id)
    admission.state = _channel_requirement_state(
        session,
        admission,
        previous_state=previous_state,
        bind_confirmation_source=bind_confirmation_source,
    )
    admission.completion_policy = admission.completion_policy or "explicit_bot_confirmation"
    session.flush()
    if bound_task_id:
        _plan_trusted_prompt_actions(
            session,
            admission=admission,
            task_id=str(bound_task_id),
            message_id=message_id,
            text=text,
            control_buttons=control_buttons,
            bind_confirmation_source=bind_confirmation_source,
        )
    return admission


def _plan_trusted_prompt_actions(
    session: Session,
    *,
    admission: GroupBotAdmission,
    task_id: str,
    message_id: str,
    text: str,
    control_buttons: tuple[object, ...] | list[object],
    bind_confirmation_source: bool,
) -> None:
    plan_required_channel_follow_actions(
        session,
        admission=admission,
        task_id=task_id,
        source_message_id=str(message_id or ""),
        control_buttons=control_buttons,
        prompt_text=text,
    )
    if bind_confirmation_source:
        plan_confirmation_button_action(
            session,
            admission=admission,
            task_id=task_id,
            source_message_id=str(message_id or ""),
            control_buttons=control_buttons,
        )


def _channel_requirement_state(
    session: Session,
    admission: GroupBotAdmission,
    *,
    previous_state: str,
    bind_confirmation_source: bool,
) -> str:
    if has_pending_required_channel_follows(session, admission=admission):
        return "required_channel_follow_pending"
    if not bind_confirmation_source and previous_state == "post_follow_visibility_probe":
        return previous_state
    return "awaiting_group_bot_confirmation"


def _record_trusted_prompt(admission: GroupBotAdmission, peer: str, message_id: str) -> bool:
    if not _record_trusted_peer(admission, peer):
        return False
    admission.source_message_id = str(message_id or "")
    return True


def _record_trusted_peer(admission: GroupBotAdmission, peer: str) -> bool:
    if not peer:
        return False
    if admission.trusted_bot_peer_id and admission.trusted_bot_peer_id != peer:
        admission.state = "blocked"
        admission.failure_code = "group_bot_multi_bot_conflict"
        return False
    admission.trusted_bot_peer_id = peer
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


def plannable_admission_account_ids(
    session: Session,
    admissions: list[GroupBotAdmission],
) -> set[int]:
    plannable = {
        int(row.account_id)
        for row in admissions
        if row.state in {READY_STATE, "post_follow_visibility_probe"}
    }
    candidates = [
        row for row in admissions
        if row.id
        and row.state == "awaiting_group_bot_confirmation"
        and not row.source_message_id
        and current_required_channel_refs(row)
    ]
    if not candidates:
        return plannable
    policy_keys = _active_explicit_policy_keys(session, candidates)
    eligible = [
        row for row in candidates
        if (row.tenant_id, row.group_id, row.trusted_bot_peer_id) in policy_keys
    ]
    refs_by_admission = {
        int(row.id): current_required_channel_refs(row) for row in eligible
    }
    completed_ids = _completed_follow_admission_ids(session, refs_by_admission)
    plannable.update(
        int(row.account_id) for row in eligible if row.id in completed_ids
    )
    return plannable


def _active_explicit_policy_keys(
    session: Session,
    admissions: list[GroupBotAdmission],
) -> set[tuple[int, int, str]]:
    rows = session.execute(
        select(
            GroupBotAdmissionPolicy.tenant_id,
            GroupBotAdmissionPolicy.group_id,
            GroupBotAdmissionPolicy.trusted_bot_peer_id,
        ).where(
            GroupBotAdmissionPolicy.tenant_id.in_({row.tenant_id for row in admissions}),
            GroupBotAdmissionPolicy.group_id.in_({row.group_id for row in admissions}),
            GroupBotAdmissionPolicy.completion_policy == "explicit_bot_confirmation",
            GroupBotAdmissionPolicy.status == "active",
        )
    ).all()
    return {(tenant_id, group_id, peer_id) for tenant_id, group_id, peer_id in rows}


def _completed_follow_admission_ids(
    session: Session,
    refs_by_admission: dict[int, tuple[str, ...]],
) -> set[int]:
    follows = session.scalars(select(GroupBotRequiredChannelFollow).where(
        GroupBotRequiredChannelFollow.admission_id.in_(refs_by_admission),
    )).all()
    successful_refs: dict[int, set[str]] = {}
    for row in follows:
        if row.status == "success":
            successful_refs.setdefault(row.admission_id, set()).add(row.channel_ref)
    return {
        admission_id for admission_id, refs in refs_by_admission.items()
        if set(refs).issubset(successful_refs.get(admission_id, set()))
    }


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
            bound = session.get(Action, row.action_id)
            if bound is not None:
                from .group_bot_requirement_recovery import replan_group_bot_requirement_action

                replacement = replan_group_bot_requirement_action(session, bound)
                if replacement is not None:
                    created.append(replacement)
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
    now = _now()
    current_source_message_id = str(source_message_id or "")
    if not current_source_message_id or current_source_message_id != str(admission.source_message_id or ""):
        return None
    if _current_confirmation_requirement_blocked(
        session,
        task_id=task.id,
        admission_id=admission.id,
        admission_version=int(admission.admission_version or 1),
        source_message_id=current_source_message_id,
    ):
        return None
    if _reconcile_open_confirmation_actions(
        session,
        task.id,
        admission.id,
        int(admission.admission_version or 1),
        current_source_message_id,
        now,
    ):
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
        session, task, int(admission.account_id), now, payload, flush=True
    )


def _current_confirmation_requirement_blocked(
    session: Session,
    *,
    task_id: str,
    admission_id: int,
    admission_version: int,
    source_message_id: str,
) -> bool:
    actions = _matching_confirmation_actions(
        session, task_id, admission_id, admission_version,
    )
    current = [
        action for action in actions
        if _confirmation_action_source_message_id(action) == source_message_id
    ]
    for action in current:
        if action.status == "success":
            return True
        if action.status not in {"failed", "closed_unknown", "unknown_after_send", "skipped"}:
            continue
        from .group_bot_requirement_recovery import replan_group_bot_requirement_action

        if replan_group_bot_requirement_action(session, action) is not None:
            return True
        return True
    return False


def _reconcile_open_confirmation_actions(
    session: Session,
    task_id: str,
    admission_id: int,
    admission_version: int,
    current_source_message_id: str,
    now: datetime,
) -> bool:
    actions = _matching_confirmation_actions(session, task_id, admission_id, admission_version)
    open_actions = [
        action
        for action in actions
        if action.status in OPEN_CONFIRMATION_ACTION_STATUSES
    ]
    current_source_actions = [
        action
        for action in open_actions
        if _confirmation_action_source_message_id(action) == current_source_message_id
    ]
    stale_source_actions = [
        action
        for action in open_actions
        if _confirmation_action_source_message_id(action) != current_source_message_id
    ]
    for action in stale_source_actions:
        if action.status == "pending":
            _skip_superseded_confirmation_action(action, now)
    if any(action.status in {"claiming", "executing"} for action in stale_source_actions):
        return True
    for action in current_source_actions[1:]:
        if action.status == "pending":
            _skip_superseded_confirmation_action(action, now)
    return bool(current_source_actions)


def confirmation_action_can_dispatch(
    session: Session,
    *,
    action: Any,
    admission_id: int,
    admission_version: int,
) -> bool:
    actions = _matching_confirmation_actions(session, action.task_id, admission_id, admission_version)
    if not any(str(candidate.id) == str(action.id) for candidate in actions):
        actions.append(action)
    current_source_message_id = _current_confirmation_source_message_id(session, admission_id)
    if not current_source_message_id or _confirmation_action_source_message_id(action) != current_source_message_id:
        return False
    actions = [
        candidate
        for candidate in actions
        if _confirmation_action_source_message_id(candidate) == current_source_message_id
    ]
    if any(candidate.status == "success" and str(candidate.id) != str(action.id) for candidate in actions):
        return False
    open_actions = [candidate for candidate in actions if candidate.status in OPEN_CONFIRMATION_ACTION_STATUSES]
    return not open_actions or str(open_actions[0].id) == str(action.id)


def discard_repeatable_recipient_confirmation(
    session: Session,
    *,
    admission: GroupBotAdmission,
    task_id: str,
) -> None:
    admission.source_message_id = ""
    _reconcile_open_confirmation_actions(
        session,
        task_id,
        admission.id,
        int(admission.admission_version or 1),
        "",
        model_now(),
    )


def _current_confirmation_source_message_id(session: Session, admission_id: int) -> str:
    admission = session.get(GroupBotAdmission, admission_id)
    return str(admission.source_message_id or "") if admission is not None else ""


def _confirmation_action_source_message_id(action: Any) -> str:
    payload = action.payload if isinstance(action.payload, dict) else {}
    return str(payload.get("source_message_id") or "")


def _matching_confirmation_actions(
    session: Session,
    task_id: str,
    admission_id: int,
    admission_version: int,
) -> list[Any]:
    from app.models import Action

    admission = session.get(GroupBotAdmission, admission_id)
    if admission is None:
        return []
    with session.no_autoflush:
        actions = list(
            session.scalars(
                select(Action)
                .where(
                    Action.tenant_id == admission.tenant_id,
                    Action.task_id == task_id,
                    Action.action_type == "group_bot_confirmation_button",
                    Action.payload["admission_id"].as_integer() == admission_id,
                    Action.payload["admission_version"].as_integer() == admission_version,
                )
                .order_by(Action.created_at.asc(), Action.id.asc())
            )
        )
    return actions


def _skip_superseded_confirmation_action(action: Any, now: datetime) -> None:
    action.status = "skipped"
    action.executed_at = now
    action.lease_owner = ""
    action.lease_expires_at = None
    action.claim_owner = ""
    action.claim_token = ""
    action.claim_expires_at = None
    action.result = {
        **(action.result or {}),
        "success": False,
        "error_code": GROUP_BOT_CONFIRMATION_SUPERSEDED_CODE,
        "error_message": "同一群管准入已存在待执行确认动作",
    }


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
    action_id: str = "",
) -> AdmissionGateDecision:
    admission = session.scalar(
        select(GroupBotAdmission)
        .where(
            GroupBotAdmission.tenant_id == tenant_id,
            GroupBotAdmission.group_id == group_id,
            GroupBotAdmission.account_id == account_id,
        )
        .with_for_update()
    )
    if not enforce:
        return AdmissionGateDecision(True, code="legacy_send_until_reviewed", state=admission.state if admission else "")
    if admission is None:
        return AdmissionGateDecision(False, code="group_bot_admission_missing", state="missing")
    close_observation_if_due(session, admission=admission)
    if admission.state == "abandoned":
        return _admission_gate_decision(admission, allowed=False, code="admission_abandoned")
    if _start_post_follow_visibility_probe(session, admission, action_id=action_id):
        return _admission_gate_decision(admission, allowed=True, code="post_follow_visibility_probe")
    if _resume_post_follow_visibility_probe(session, admission, action_id=action_id):
        return _admission_gate_decision(admission, allowed=True, code="post_follow_visibility_probe")
    if admission.state != READY_STATE:
        return _admission_gate_decision(admission, allowed=False, code="group_bot_admission_wait")
    return _admission_gate_decision(admission, allowed=True, code="ready")


def _admission_gate_decision(
    admission: GroupBotAdmission,
    *,
    allowed: bool,
    code: str,
) -> AdmissionGateDecision:
    return AdmissionGateDecision(
        allowed,
        code=code,
        admission_id=admission.id,
        admission_version=int(admission.admission_version or 1),
        state=admission.state,
    )


def _start_post_follow_visibility_probe(
    session: Session,
    admission: GroupBotAdmission,
    *,
    action_id: str,
) -> bool:
    if admission.state != "awaiting_group_bot_confirmation":
        return False
    if admission.account_id not in plannable_admission_account_ids(
        session, [admission],
    ):
        return False
    admission.state = "post_follow_visibility_probe"
    admission.post_send_visibility_state = "pending"
    _bind_post_follow_probe_action(admission, action_id)
    session.flush()
    return True


def _resume_post_follow_visibility_probe(
    session: Session,
    admission: GroupBotAdmission,
    *,
    action_id: str,
) -> bool:
    if admission.state != "post_follow_visibility_probe" or not action_id:
        return False
    observation = dict(admission.transport_observation or {})
    bound_action_id = str(observation.get(POST_FOLLOW_PROBE_ACTION_KEY) or "")
    if bound_action_id and bound_action_id != action_id:
        if not _pre_gateway_probe_binding_reclaimable(session, bound_action_id):
            return False
    _bind_post_follow_probe_action(admission, action_id)
    session.flush()
    return True


def _bind_post_follow_probe_action(admission: GroupBotAdmission, action_id: str) -> None:
    if not action_id:
        return
    observation = dict(admission.transport_observation or {})
    observation[POST_FOLLOW_PROBE_ACTION_KEY] = str(action_id)
    admission.transport_observation = observation


def _pre_gateway_probe_binding_reclaimable(session: Session, action_id: str) -> bool:
    action = session.get(Action, action_id)
    if action is None:
        return True
    if action.status not in PRE_GATEWAY_TERMINAL_ACTION_STATUSES:
        return False
    gateway_started = session.scalar(
        select(ExecutionAttempt.id)
        .where(
            ExecutionAttempt.action_id == action_id,
            ExecutionAttempt.gateway_call_started_at.is_not(None),
        )
        .limit(1)
    )
    return gateway_started is None


def needs_post_send_visibility(admission: GroupBotAdmission | None, *, action_admission_version: int | None) -> bool:
    if admission is None:
        return False
    if (
        admission.state in LEGACY_VISIBILITY_PROBE_STATES
        and not any(
            (
                str(admission.trusted_bot_peer_id or ""),
                str(admission.source_message_id or ""),
                str(admission.evidence_ref or ""),
            )
        )
    ):
        return admission.post_send_visibility_state != "visible_confirmed"
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
    action = session.get(Action, action_id)
    payload = action.payload if action and isinstance(action.payload, dict) else {}
    quantity_slot_id = str(action.primary_quantity_slot_id or "") if action else ""
    quantity_slot = session.get(TaskGroupDailyMessageSlot, quantity_slot_id) if quantity_slot_id else None
    row = PendingVisibilityCredit(
        tenant_id=tenant_id,
        action_id=action_id,
        bucket_id=bucket_id,
        task_day_ledger_id=quantity_slot.task_day_ledger_id if quantity_slot else None,
        primary_quantity_slot_id=quantity_slot_id or None,
        task_account_daily_coverage_id=str(payload.get("coverage_ledger_id") or "") or None,
        execution_attempt_id=execution_attempt_id,
        remote_message_id=str(remote_message_id or ""),
        admission_version=int(payload.get("group_bot_admission_version") or 0) or None,
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
    if admission.state == "post_follow_visibility_probe":
        admission.state = READY_STATE
        admission.completion_policy = "post_follow_visibility_confirmed"
        admission.failure_code = ""
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
    "plannable_admission_account_ids",
    "attribute_prompt_to_account",
    "ingest_trusted_bot_prompt",
    "plan_required_channel_follow_actions",
    "discard_repeatable_recipient_confirmation",
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

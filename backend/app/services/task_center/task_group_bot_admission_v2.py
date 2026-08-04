from __future__ import annotations
from dataclasses import dataclass
from datetime import timedelta
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import (
    Action,
    Task,
    TaskGroupBotAdmission,
    TgAccount,
    TgGroup,
)
from app.services._common import _now, gateway
from app.services.developer_apps import credentials_for_account
from app.timezone import as_beijing
from .task_group_bot_admission_surface import (
    current_authorization as _current_authorization,
    fact_hash as _hash,
    latest_group_cursor as _latest_group_cursor,
    max_cursor as _max_cursor,
    numeric_cursor as _numeric_cursor,
    surface_identity as _surface_identity,
    surface_is_current,
)
from .task_group_bot_admission_facts import record_fact as _record_fact
from .task_group_bot_admission_insert import persist_unique_observation
from .task_group_bot_admission_prompts import record_control_facts as _record_control_facts
OBSERVATION_SECONDS = 30
@dataclass(frozen=True)
class AdmissionDecision:
    allowed: bool
    code: str
    admission_id: str
    version: int
@dataclass(frozen=True)
class ProbeSurface:
    account: TgAccount
    group: TgGroup
    authorization: object
    identity: dict
    start_cursor: int | None


def evaluate_task_admission(
    session: Session,
    *,
    task_id: str,
    tenant_id: int,
    group_id: int,
    account_id: int,
) -> AdmissionDecision:
    admission = _admission(session, task_id, group_id, account_id=account_id)
    if admission is None:
        admission = _start_observation(
            session,
            task_id=task_id,
            tenant_id=tenant_id,
            group_id=group_id,
            account_id=account_id,
        )
        if admission.state == "ready":
            return _decision(admission, True, "c2_existing_account_group_fact")
        if admission.state == "abandoned":
            return _decision(admission, False, "c2_account_abandoned")
        return _decision(admission, False, "c2_observation_started")
    if admission.state == "ready":
        return _decision(admission, True, "c2_ready")
    if admission.state == "requirements_pending":
        return _requirements_decision(session, admission)
    if admission.state == "abandoned":
        return _decision(admission, False, "c2_account_abandoned")
    if admission.observation_gap:
        return _decision(admission, False, "c2_observation_gap")
    if as_beijing(admission.no_prompt_pass_at) > _now():
        return _decision(admission, False, "c2_observing_30s")
    return _probe_due_observation(session, admission)


def _probe_due_observation(
    session: Session,
    admission: TaskGroupBotAdmission,
) -> AdmissionDecision:
    probe = _load_probe_surface(session, admission)
    if isinstance(probe, AdmissionDecision):
        return probe
    messages = _fetch_surface_messages(session, admission, probe)
    if isinstance(messages, AdmissionDecision):
        return messages
    current_authorization = _current_authorization(session, admission.account_id)
    if current_authorization is None or not surface_is_current(
        probe.identity,
        probe.group,
        current_authorization,
        account_id=admission.account_id,
    ):
        return _restart_surface(
            session,
            admission=admission,
            group=probe.group,
            authorization=current_authorization,
        )
    end_cursor = _max_cursor(messages, probe.start_cursor)
    if messages and _record_control_facts(
        session,
        admission,
        messages,
        end_cursor=end_cursor,
    ):
        return _decision(admission, False, "c2_requirements_pending")
    return _confirm_no_prompt(session, admission, end_cursor)


def _load_probe_surface(
    session: Session,
    admission: TaskGroupBotAdmission,
) -> ProbeSurface | AdmissionDecision:
    account = session.get(TgAccount, admission.account_id)
    group = session.get(TgGroup, admission.target_group_id)
    if account is None or not account.session_ciphertext:
        return _abandon(session, admission, "session_unavailable")
    if group is None:
        return _abandon(session, admission, "target_group_unavailable")
    authorization = _current_authorization(session, admission.account_id)
    if authorization is None:
        return _abandon(session, admission, "current_authorization_missing")
    surface = dict(admission.surface_identity or {})
    if not surface_is_current(
        surface,
        group,
        authorization,
        account_id=admission.account_id,
    ):
        return _restart_surface(
            session,
            admission=admission,
            group=group,
            authorization=authorization,
        )
    return ProbeSurface(
        account,
        group,
        authorization,
        surface,
        _numeric_cursor(surface.get("observed_start_cursor")),
    )


def _fetch_surface_messages(
    session: Session,
    admission: TaskGroupBotAdmission,
    probe: ProbeSurface,
) -> list | AdmissionDecision:
    try:
        return gateway.fetch_group_messages(
            probe.account.id,
            probe.group.tg_peer_id,
            probe.account.session_ciphertext,
            credentials_for_account(session, probe.account),
            limit=25,
            control_only=True,
            after_message_id=probe.start_cursor,
        )
    except Exception as exc:
        if _unusable_telegram_error(exc):
            return _abandon(session, admission, str(exc) or type(exc).__name__)
        return _restart_with_gap(session, admission, type(exc).__name__)


def _start_observation(
    session: Session,
    *,
    task_id: str,
    tenant_id: int,
    group_id: int,
    account_id: int,
) -> TaskGroupBotAdmission:
    group = session.get(TgGroup, group_id)
    if group is None:
        raise ValueError("c2_target_group_missing")
    authorization = _current_authorization(session, account_id)
    cursor = _latest_group_cursor(session, group_id)
    started_at = _now()
    identity = _surface_identity(
        group,
        authorization=authorization,
        account_id=account_id,
        start_cursor=cursor,
        end_cursor=cursor,
        observation_version=1,
    )
    imported_ready = _legacy_admission_ready(
        session,
        tenant_id,
        group_id,
        account_id=account_id,
    )
    row = _new_observation_row(
        task_id=task_id,
        tenant_id=tenant_id,
        group_id=group_id,
        account_id=account_id,
        authorization=authorization,
        imported_ready=imported_ready,
        started_at=started_at,
        identity=identity,
        task_lifecycle_epoch=_task_lifecycle_epoch(session, task_id),
    )
    row, created = persist_unique_observation(session, row)
    if not created:
        return row
    _record_initial_observation_fact(
        session,
        row,
        authorization=authorization,
        imported_ready=imported_ready,
    )
    return row


def _new_observation_row(
    *,
    task_id: str,
    tenant_id: int,
    group_id: int,
    account_id: int,
    authorization,
    imported_ready: bool,
    started_at,
    identity: dict,
    task_lifecycle_epoch: int,
) -> TaskGroupBotAdmission:
    return TaskGroupBotAdmission(
        tenant_id=tenant_id,
        task_id=task_id,
        task_lifecycle_epoch=task_lifecycle_epoch,
        account_id=account_id,
        target_group_id=group_id,
        state="abandoned" if authorization is None else "ready" if imported_ready else "observing",
        observation_started_at=started_at,
        no_prompt_pass_at=started_at + timedelta(seconds=OBSERVATION_SECONDS),
        observation_gap=False,
        surface_identity_hash=_hash(identity),
        surface_identity=identity,
    )


def _record_initial_observation_fact(
    session: Session,
    row: TaskGroupBotAdmission,
    *,
    authorization,
    imported_ready: bool,
) -> None:
    if authorization is None:
        row.terminal_reason = "current_authorization_missing"
        row.terminal_evidence = {"outcome": "abandoned_for_task"}
        return
    if imported_ready:
        _record_fact(session, row, "post_follow_visibility", outcome={
            "outcome": "existing_visible_confirmed_fact",
            "surface_identity_hash": row.surface_identity_hash,
        })


def _legacy_admission_ready(
    session: Session,
    tenant_id: int,
    group_id: int,
    *,
    account_id: int,
) -> bool:
    from app.models import GroupBotAdmission

    row = session.scalar(select(GroupBotAdmission).where(
        GroupBotAdmission.tenant_id == tenant_id,
        GroupBotAdmission.group_id == group_id,
        GroupBotAdmission.account_id == account_id,
    ))
    return bool(
        row
        and row.state == "group_bot_admission_ready"
        and row.post_send_visibility_state == "visible_confirmed"
    )


def _task_lifecycle_epoch(session: Session, task_id: str) -> int:
    task = session.get(Task, task_id)
    if task is None:
        raise ValueError("c2_task_missing")
    return int(task.task_lifecycle_epoch or 1)


def _confirm_no_prompt(
    session: Session,
    admission: TaskGroupBotAdmission,
    end_cursor: int,
) -> AdmissionDecision:
    identity = dict(admission.surface_identity or {})
    identity["observed_end_cursor"] = str(end_cursor)
    version = int(admission.version or 1)
    changed = session.execute(
        update(TaskGroupBotAdmission)
        .where(
            TaskGroupBotAdmission.id == admission.id,
            TaskGroupBotAdmission.state == "observing",
            TaskGroupBotAdmission.version == version,
            TaskGroupBotAdmission.observation_gap.is_(False),
        )
        .values(
            state="ready",
            surface_identity=identity,
            surface_identity_hash=_hash(identity),
            version=version + 1,
        )
    ).rowcount
    if changed != 1:
        raise ValueError("c2_observation_version_conflict")
    _record_fact(session, admission, "post_follow_visibility", outcome={
        "outcome": "no_prompt_30s_passed",
        "surface_identity_hash": _hash(identity),
    })
    session.refresh(admission)
    return _decision(admission, True, "c2_no_prompt_30s_passed")


def _requirements_decision(
    session: Session,
    admission: TaskGroupBotAdmission,
) -> AdmissionDecision:
    actions = _requirement_actions(session, admission)
    if not actions:
        return _decision(admission, False, "c2_requirement_actions_missing")
    if any(action.status in {"failed", "skipped"} for action in actions):
        admission.state = "abandoned"
        admission.version = int(admission.version or 1) + 1
        return _decision(admission, False, "c2_account_abandoned")
    if any(action.status in {"pending", "claiming", "executing"} for action in actions):
        return _decision(admission, False, "c2_requirements_pending")
    _record_requirement_success_facts(session, admission, actions)
    _start_post_follow_observation(admission)
    return _decision(admission, False, "c2_post_follow_observing_30s")


def _requirement_actions(
    session: Session,
    admission: TaskGroupBotAdmission,
) -> list[Action]:
    return list(session.scalars(
        select(Action).where(
            Action.task_id == admission.task_id,
            Action.account_id == admission.account_id,
            Action.action_type.in_((
                "group_bot_channel_follow",
                "group_bot_confirmation_button",
            )),
            Action.created_at >= admission.observation_started_at,
        )
    ))


def _record_requirement_success_facts(
    session: Session,
    admission: TaskGroupBotAdmission,
    actions: list[Action],
) -> None:
    for action in actions:
        kind = (
            "requirement_confirmation"
            if action.action_type == "group_bot_confirmation_button"
            else "dynamic_channel_follow"
        )
        _record_fact(session, admission, kind, outcome={
            "action_id": action.id,
            "action_status": action.status,
            "result": dict(action.result or {}),
        })


def _start_post_follow_observation(admission: TaskGroupBotAdmission) -> None:
    now_value = _now()
    identity = dict(admission.surface_identity or {})
    identity["observed_start_cursor"] = identity.get("observed_end_cursor", "1")
    identity["listener_instance_epoch"] = int(admission.observation_version or 1) + 1
    admission.state = "observing"
    admission.observation_version = int(admission.observation_version or 1) + 1
    admission.observation_started_at = now_value
    admission.no_prompt_pass_at = now_value + timedelta(seconds=OBSERVATION_SECONDS)
    admission.surface_identity = identity
    admission.surface_identity_hash = _hash(identity)
    admission.version = int(admission.version or 1) + 1


def _restart_with_gap(
    session: Session,
    admission: TaskGroupBotAdmission,
    reason: str,
) -> AdmissionDecision:
    now_value = _now()
    _record_fact(session, admission, "post_follow_visibility", outcome={
        "outcome": "observation_gap",
        "reason": reason,
    })
    admission.observation_gap = False
    admission.state = "observing"
    admission.observation_version = int(admission.observation_version or 1) + 1
    admission.version = int(admission.version or 1) + 1
    admission.observation_started_at = now_value
    admission.no_prompt_pass_at = now_value + timedelta(seconds=OBSERVATION_SECONDS)
    identity = dict(admission.surface_identity or {})
    identity["listener_instance_epoch"] = admission.observation_version
    identity["gap_reason"] = reason
    admission.surface_identity = identity
    admission.surface_identity_hash = _hash(identity)
    return _decision(admission, False, "c2_observation_gap")


def _restart_surface(
    session,
    *,
    admission,
    group,
    authorization,
) -> AdmissionDecision:
    if authorization is None:
        return _abandon(session, admission, "current_authorization_missing")
    now_value = _now()
    cursor = _latest_group_cursor(session, group.id)
    identity = _surface_identity(
        group,
        authorization=authorization,
        account_id=admission.account_id,
        start_cursor=cursor,
        end_cursor=cursor,
        observation_version=int(admission.observation_version or 1) + 1,
    )
    _record_fact(session, admission, "post_follow_visibility", outcome={
        "outcome": "observation_surface_changed",
        "previous_surface_identity_hash": admission.surface_identity_hash,
    })
    admission.state = "observing"
    admission.observation_version = int(admission.observation_version or 1) + 1
    admission.observation_started_at = now_value
    admission.no_prompt_pass_at = now_value + timedelta(seconds=OBSERVATION_SECONDS)
    admission.surface_identity = identity
    admission.surface_identity_hash = _hash(identity)
    admission.version = int(admission.version or 1) + 1
    return _decision(admission, False, "c2_observation_surface_changed")


def _abandon(
    session: Session,
    admission: TaskGroupBotAdmission,
    reason: str,
) -> AdmissionDecision:
    admission.state = "abandoned"
    admission.terminal_reason = reason[:80]
    admission.terminal_evidence = {
        "outcome": "abandoned_for_task",
        "detail": reason[:160],
    }
    admission.version = int(admission.version or 1) + 1
    return _decision(admission, False, "c2_account_abandoned")


def _unusable_telegram_error(exc: Exception) -> bool:
    detail = f"{type(exc).__name__}:{exc}".lower()
    return any(code in detail for code in (
        "session_revoked",
        "session_unauthorized",
        "auth_key_unregistered",
        "need_relogin",
        "user_deactivated",
        "account_banned",
        "channel_invalid",
        "peer_id_invalid",
    ))


def _admission(
    session: Session,
    task_id: str,
    group_id: int,
    *,
    account_id: int,
) -> TaskGroupBotAdmission | None:
    return session.scalar(select(TaskGroupBotAdmission).where(
        TaskGroupBotAdmission.task_id == task_id,
        TaskGroupBotAdmission.target_group_id == group_id,
        TaskGroupBotAdmission.account_id == account_id,
    ))
def _decision(
    admission: TaskGroupBotAdmission,
    allowed: bool,
    code: str,
) -> AdmissionDecision:
    return AdmissionDecision(allowed, code, admission.id, int(admission.version or 1))


__all__ = ["AdmissionDecision", "evaluate_task_admission"]

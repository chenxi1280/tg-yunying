"""Observation transitions and task-day failure evidence for C2 admission."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.models import TaskGroupBotAdmission
from app.services._common import _now

from .task_group_bot_admission_facts import record_fact
from .task_group_bot_admission_surface import fact_hash, latest_group_cursor, surface_identity


OBSERVATION_SECONDS = 30
MAX_OBSERVATION_GAP_RETRIES = 3
OBSERVATION_GAP_LIMIT_REASON = "observation_gap_limit_reached"
TASK_DAY_TERMINAL_REASONS = ("target_entity_unresolvable", OBSERVATION_GAP_LIMIT_REASON)


@dataclass(frozen=True)
class AdmissionDecision:
    allowed: bool
    code: str
    admission_id: str
    version: int
    terminal_reason: str = ""


def decision(admission: TaskGroupBotAdmission, allowed: bool, code: str) -> AdmissionDecision:
    return AdmissionDecision(
        allowed, code, admission.id, int(admission.version or 1), str(admission.terminal_reason or ""),
    )


def _transition(session: Session, admission: TaskGroupBotAdmission, values: dict) -> None:
    version = int(admission.version or 1)
    changed = session.execute(update(TaskGroupBotAdmission).where(
        TaskGroupBotAdmission.id == admission.id,
        TaskGroupBotAdmission.version == version,
    ).values(**values, version=version + 1)).rowcount
    if changed != 1:
        raise ValueError("c2_observation_version_conflict")
    session.refresh(admission)


def restart_with_gap(session: Session, admission: TaskGroupBotAdmission, reason: str) -> AdmissionDecision:
    timestamp = _now()
    count = int(admission.consecutive_observation_gaps or 0) + 1
    observation_version = int(admission.observation_version or 1) + 1
    identity = {**dict(admission.surface_identity or {}),
                "listener_instance_epoch": observation_version, "gap_reason": reason}
    values = {
        "consecutive_observation_gaps": count, "observation_gap": False,
        "observation_version": observation_version, "state": "observing",
        "observation_started_at": timestamp,
        "no_prompt_pass_at": timestamp + timedelta(seconds=OBSERVATION_SECONDS),
        "surface_identity": identity, "surface_identity_hash": fact_hash(identity),
    }
    if count >= MAX_OBSERVATION_GAP_RETRIES:
        values.update(_terminal_values(OBSERVATION_GAP_LIMIT_REASON, detail=reason, count=count))
    _transition(session, admission, values)
    record_fact(session, admission, "post_follow_visibility", outcome={
        "outcome": "observation_gap", "reason": reason, "consecutive_observation_gaps": count,
        "task_id": admission.task_id, "observation_version": observation_version,
    })
    code = "c2_account_abandoned" if admission.state == "abandoned" else "c2_observation_gap"
    return decision(admission, False, code)


def restart_surface(session, *, admission, group, account, authorization) -> AdmissionDecision:
    if account is None or not account.session_ciphertext:
        return abandon(session, admission, "session_unavailable")
    timestamp = _now()
    cursor = latest_group_cursor(session, group.id)
    observation_version = int(admission.observation_version or 1) + 1
    identity = surface_identity(
        group, authorization=authorization, account_id=admission.account_id,
        session_ciphertext=str(account.session_ciphertext), start_cursor=cursor,
        end_cursor=cursor, observation_version=observation_version,
    )
    previous_hash = admission.surface_identity_hash
    _transition(session, admission, {
        "state": "observing", "observation_version": observation_version,
        "observation_started_at": timestamp,
        "no_prompt_pass_at": timestamp + timedelta(seconds=OBSERVATION_SECONDS),
        "surface_identity": identity, "surface_identity_hash": fact_hash(identity),
        "terminal_reason": "", "terminal_evidence": {},
        "consecutive_observation_gaps": 0, "observation_gap": False,
    })
    record_fact(session, admission, "post_follow_visibility", outcome={
        "outcome": "observation_surface_changed", "previous_surface_identity_hash": previous_hash,
    })
    return decision(admission, False, "c2_observation_surface_changed")


def _terminal_values(reason: str, *, detail: str = "", count: int = 0) -> dict:
    evidence = {
        "outcome": "abandoned_for_day" if reason in TASK_DAY_TERMINAL_REASONS else "abandoned_for_task",
        "detail": (detail or reason)[:160], "terminal_date": _now().date().isoformat(),
    }
    if reason == OBSERVATION_GAP_LIMIT_REASON:
        evidence.update(consecutive_observation_gaps=count, blocker_code="c2_observation_evidence_missing")
    return {"state": "abandoned", "terminal_reason": reason[:80], "terminal_evidence": evidence}


def abandon(session: Session, admission: TaskGroupBotAdmission, reason: str) -> AdmissionDecision:
    _transition(session, admission, _terminal_values(
        reason, count=int(admission.consecutive_observation_gaps or 0),
    ))
    return decision(admission, False, "c2_account_abandoned")


def task_day_terminal_expired(admission: TaskGroupBotAdmission) -> bool:
    if admission.terminal_reason not in TASK_DAY_TERMINAL_REASONS:
        return False
    terminal_date = date.fromisoformat(str((admission.terminal_evidence or {}).get("terminal_date") or ""))
    return terminal_date < _now().date()


def start_post_follow_observation(admission: TaskGroupBotAdmission) -> None:
    now_value = _now()
    identity = dict(admission.surface_identity or {})
    identity["observed_start_cursor"] = identity.get("observed_end_cursor", "1")
    identity["listener_instance_epoch"] = int(admission.observation_version or 1) + 1
    admission.state = "observing"
    admission.consecutive_observation_gaps = 0
    admission.observation_version = int(admission.observation_version or 1) + 1
    admission.observation_started_at = now_value
    admission.no_prompt_pass_at = now_value + timedelta(seconds=OBSERVATION_SECONDS)
    admission.surface_identity = identity
    admission.surface_identity_hash = fact_hash(identity)
    admission.version = int(admission.version or 1) + 1

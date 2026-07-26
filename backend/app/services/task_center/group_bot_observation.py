"""Auditable listener observation helpers for group-bot admission."""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import GroupBotAdmission, GroupBotAdmissionObservation, GroupContextMessage, TgGroup
from app.models.enums import now as model_now


CONTEXT_CURSOR_SCAN_LIMIT = 500
DEFAULT_OBSERVATION_WINDOW_SECONDS = 120
OBSERVING_STATES = ("awaiting_group_bot_rule", "observation_open")
RESTARTABLE_STATES = (*OBSERVING_STATES, "observation_stale", "group_bot_policy_unresolved", "group_bot_rule_unattributed")


def numeric_cursor(value: object) -> int | None:
    text = str(value or "").strip()
    if not text.isdigit():
        return None
    cursor = int(text)
    return cursor if cursor > 0 else None


def max_snapshot_cursor(snapshots: Iterable[object]) -> str:
    _, upper = snapshot_cursor_bounds(snapshots)
    return str(upper) if upper is not None else ""


def snapshot_cursor_bounds(snapshots: Iterable[object]) -> tuple[int | None, int | None]:
    cursors = [
        cursor
        for snapshot in snapshots
        if (cursor := numeric_cursor(getattr(snapshot, "remote_message_id", ""))) is not None
    ]
    return (min(cursors), max(cursors)) if cursors else (None, None)


def observing_admissions(session: Session, *, group: TgGroup) -> list[GroupBotAdmission]:
    stmt = select(GroupBotAdmission).where(
        GroupBotAdmission.tenant_id == group.tenant_id,
        GroupBotAdmission.group_id == group.id,
        GroupBotAdmission.state.in_(OBSERVING_STATES),
    )
    return list(session.scalars(stmt))


def record_listener_observations(
    session: Session,
    *,
    group: TgGroup,
    listener_account_id: int,
    snapshots: Iterable[object],
    failure_code: str = "",
) -> int:
    snapshot_rows = list(snapshots)
    lower, upper = snapshot_cursor_bounds(snapshot_rows)
    admissions = observing_admissions(session, group=group)
    for admission in admissions:
        _record_listener_observation(
            session,
            admission=admission,
            listener_account_id=listener_account_id,
            read_count=len(snapshot_rows),
            lower_cursor=lower,
            upper_cursor=upper,
            failure_code=failure_code,
        )
    return len(admissions)


def _record_listener_observation(
    session: Session,
    *,
    admission: GroupBotAdmission,
    listener_account_id: int,
    read_count: int,
    lower_cursor: int | None,
    upper_cursor: int | None,
    failure_code: str,
) -> None:
    baseline = numeric_cursor(admission.join_start_cursor)
    cursor_gap, observation_failure = _observation_failure(
        baseline=baseline,
        lower_cursor=lower_cursor,
        upper_cursor=upper_cursor,
        failure_code=failure_code,
    )
    record_observation_batch(
        session,
        admission=admission,
        observed_end_cursor=str(upper_cursor or ""),
        listener_account_id=listener_account_id,
        read_count=read_count,
        cursor_gap=cursor_gap,
        failure_code=observation_failure,
        result_summary={
            "source": "listener_poll",
            "lower_cursor": str(lower_cursor or ""),
            "upper_cursor": str(upper_cursor or ""),
        },
    )


def _observation_failure(
    *,
    baseline: int | None,
    lower_cursor: int | None,
    upper_cursor: int | None,
    failure_code: str,
) -> tuple[bool, str]:
    if failure_code:
        return True, failure_code
    if baseline is None:
        return True, "join_start_cursor_missing"
    if lower_cursor is None or upper_cursor is None:
        return True, "observation_cursor_missing"
    if upper_cursor < baseline:
        return True, "observed_end_before_join_start"
    if lower_cursor > baseline:
        return True, "cursor_gap"
    return False, ""


def record_observation_batch(
    session: Session,
    *,
    admission: GroupBotAdmission,
    observed_end_cursor: str,
    listener_account_id: int | None = None,
    read_count: int = 0,
    cursor_gap: bool = False,
    failure_code: str = "",
    result_summary: dict[str, Any] | None = None,
) -> GroupBotAdmissionObservation:
    observation = GroupBotAdmissionObservation(
        admission_id=admission.id,
        join_start_cursor=str(admission.join_start_cursor or ""),
        observed_end_cursor=str(observed_end_cursor or ""),
        listener_account_id=listener_account_id,
        read_count=int(read_count or 0),
        cursor_gap=bool(cursor_gap),
        failure_code=str(failure_code or ""),
        observation_version=int(admission.admission_version or 1),
        result_summary=dict(result_summary or {}),
    )
    session.add(observation)
    if failure_code or cursor_gap:
        admission.state = "observation_stale"
        admission.failure_code = failure_code or "cursor_gap"
    else:
        admission.observed_end_cursor = str(observed_end_cursor or admission.observed_end_cursor or "")
    session.flush()
    return observation


def has_valid_observation(session: Session, *, admission: GroupBotAdmission) -> bool:
    observation = session.scalar(
        select(GroupBotAdmissionObservation)
        .where(
            GroupBotAdmissionObservation.admission_id == admission.id,
            GroupBotAdmissionObservation.observation_version == int(admission.admission_version or 1),
        )
        .order_by(GroupBotAdmissionObservation.id.desc())
        .limit(1)
    )
    if observation is None or observation.cursor_gap or observation.failure_code:
        return False
    baseline = numeric_cursor(observation.join_start_cursor)
    observed_end = numeric_cursor(observation.observed_end_cursor)
    return baseline is not None and observed_end is not None and observed_end >= baseline


def restart_admission_observation(
    session: Session,
    *,
    admission: GroupBotAdmission,
    expected_admission_version: int,
    reason: str,
    evidence_ref: str,
) -> GroupBotAdmission:
    _assert_restartable(admission, expected_admission_version)
    cursor = latest_persisted_group_cursor(session, group_id=admission.group_id)
    if not cursor:
        raise ValueError("join_start_cursor_missing")
    now = model_now()
    admission.state = "awaiting_group_bot_rule"
    admission.admission_version = int(admission.admission_version or 1) + 1
    admission.join_start_cursor = cursor
    admission.observed_end_cursor = ""
    admission.failure_code = ""
    admission.evidence_ref = str(evidence_ref or "")
    admission.join_success_at = now
    admission.observation_closes_at = now + timedelta(seconds=DEFAULT_OBSERVATION_WINDOW_SECONDS)
    admission.required_channel_refs = []
    admission.trusted_bot_peer_id = ""
    admission.completion_policy = ""
    admission.policy_version = 0
    admission.abandoned_reason = ""
    session.flush()
    return admission


def _assert_restartable(admission: GroupBotAdmission, expected_admission_version: int) -> None:
    if int(admission.admission_version or 1) != int(expected_admission_version):
        raise ValueError("admission_version_conflict")
    if admission.state not in RESTARTABLE_STATES:
        raise ValueError("admission_restart_not_allowed")


def latest_persisted_group_cursor(session: Session, *, group_id: int) -> str:
    rows = session.scalars(
        select(GroupContextMessage.remote_message_id)
        .where(GroupContextMessage.group_id == group_id)
        .order_by(GroupContextMessage.id.desc())
        .limit(CONTEXT_CURSOR_SCAN_LIMIT)
    )
    cursors = [cursor for value in rows if (cursor := numeric_cursor(value)) is not None]
    return str(max(cursors)) if cursors else ""


__all__ = [
    "OBSERVING_STATES",
    "has_valid_observation",
    "latest_persisted_group_cursor",
    "max_snapshot_cursor",
    "numeric_cursor",
    "observing_admissions",
    "record_listener_observations",
    "record_observation_batch",
    "restart_admission_observation",
    "snapshot_cursor_bounds",
]

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import (
    AccountStatus,
    Task,
    TaskAccountDailyCoverage,
    TaskGroupBotAdmission,
    TgAccount,
    TgGroup,
)
from app.services._common import _now

from .task_group_bot_admission_facts import record_fact
from .task_group_bot_admission_surface import (
    current_authorization,
    fact_hash,
    latest_group_cursor,
    surface_identity,
)


OBSERVATION_SECONDS = 30
UNPROVEN_TERMINAL_REASON = "current_authorization_missing"


def restart_unproven_admission(
    session: Session,
    admission: TaskGroupBotAdmission,
) -> bool:
    return _restart_admission_observation(
        session,
        admission,
        outcome="unproven_local_terminal_reopened",
    )


def restart_task_day_admission(
    session: Session,
    admission: TaskGroupBotAdmission,
) -> bool:
    return _restart_admission_observation(
        session,
        admission,
        outcome="target_entity_recheck_on_new_task_day",
    )


def restart_stale_confirmation_observation(
    session: Session,
    admission: TaskGroupBotAdmission,
) -> bool:
    return _restart_admission_observation(
        session,
        admission,
        outcome="group_bot_confirmation_source_stale",
    )


def _restart_admission_observation(
    session: Session,
    admission: TaskGroupBotAdmission,
    *,
    outcome: str,
) -> bool:
    account = session.get(TgAccount, admission.account_id)
    group = session.get(TgGroup, admission.target_group_id)
    if not _account_can_be_observed(account) or group is None:
        return False
    authorization = current_authorization(session, admission.account_id)
    now_value = _now()
    cursor = latest_group_cursor(session, group.id)
    identity = surface_identity(
        group,
        authorization=authorization,
        account_id=admission.account_id,
        session_ciphertext=str(account.session_ciphertext),
        start_cursor=cursor,
        end_cursor=cursor,
        observation_version=int(admission.observation_version or 1) + 1,
    )
    expected_version = int(admission.version or 1)
    updated_id = session.scalar(
        update(TaskGroupBotAdmission)
        .where(
            TaskGroupBotAdmission.id == admission.id,
            TaskGroupBotAdmission.version == expected_version,
        )
        .values(**_restart_values(admission, identity=identity, now_value=now_value))
        .returning(TaskGroupBotAdmission.id)
    )
    session.refresh(admission)
    if updated_id is None:
        return False
    record_fact(session, admission, "post_follow_visibility", outcome={
        "outcome": outcome,
        "surface_identity_hash": admission.surface_identity_hash,
    })
    return True


def reopen_unproven_task_coverages(
    session: Session,
    task: Task,
    group: TgGroup,
    *,
    limit: int,
) -> int:
    admissions = list(session.scalars(
        select(TaskGroupBotAdmission)
        .join(TgAccount, TgAccount.id == TaskGroupBotAdmission.account_id)
        .where(
            TaskGroupBotAdmission.task_id == task.id,
            TaskGroupBotAdmission.target_group_id == group.id,
            TaskGroupBotAdmission.state == "abandoned",
            TaskGroupBotAdmission.terminal_reason == UNPROVEN_TERMINAL_REASON,
            TgAccount.deleted_at.is_(None),
            TgAccount.status == AccountStatus.ACTIVE.value,
            TgAccount.session_ciphertext.is_not(None),
            TgAccount.session_ciphertext != "",
        )
        .order_by(TaskGroupBotAdmission.account_id)
        .limit(max(1, limit))
    ))
    reopened_ids = {
        row.account_id for row in admissions
        if restart_unproven_admission(session, row)
    }
    if not reopened_ids:
        return 0
    _reopen_coverages(session, task, group, reopened_ids)
    return len(reopened_ids)


def _account_can_be_observed(account: TgAccount | None) -> bool:
    return bool(
        account
        and account.deleted_at is None
        and account.status == AccountStatus.ACTIVE.value
        and account.session_ciphertext
    )


def _restart_values(admission, *, identity: dict, now_value) -> dict[str, object]:
    return {
        "state": "observing",
        "observation_version": int(admission.observation_version or 1) + 1,
        "observation_started_at": now_value,
        "no_prompt_pass_at": now_value + timedelta(seconds=OBSERVATION_SECONDS),
        "observation_gap": False,
        "surface_identity": identity,
        "surface_identity_hash": fact_hash(identity),
        "terminal_reason": "",
        "terminal_evidence": {},
        "version": int(admission.version or 1) + 1,
    }


def _reopen_coverages(
    session: Session,
    task: Task,
    group: TgGroup,
    account_ids: set[int],
) -> None:
    rows = session.scalars(select(TaskAccountDailyCoverage).where(
        TaskAccountDailyCoverage.task_id == task.id,
        TaskAccountDailyCoverage.group_id == group.id,
        TaskAccountDailyCoverage.coverage_date == _now().date(),
        TaskAccountDailyCoverage.account_id.in_(account_ids),
        TaskAccountDailyCoverage.state == "abandoned_for_day",
        TaskAccountDailyCoverage.blocker_code == "account_task_abandoned",
    ))
    for row in rows:
        row.state = "pending_admission"
        row.blocker_code = "group_bot_admission_wait"
        row.blocker_stage = "admission"
        row.blocker_detail = "本地授权投影缺失不是 Telegram 终态，已重开账号观察"
        row.recovery_path = "task_group_bot_observation"
        row.next_eligible_at = _now()


__all__ = [
    "reopen_unproven_task_coverages",
    "restart_task_day_admission",
    "restart_stale_confirmation_observation",
    "restart_unproven_admission",
]

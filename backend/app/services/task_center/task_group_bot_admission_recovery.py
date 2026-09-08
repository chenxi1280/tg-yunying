from __future__ import annotations

from datetime import timedelta

from sqlalchemy import and_, or_, select, update
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
from .task_group_bot_admission_state import (
    OBSERVATION_SECONDS, TASK_DAY_TERMINAL_REASONS, task_day_terminal_expired,
)
from .task_group_bot_admission_surface import (
    current_authorization,
    fact_hash,
    latest_group_cursor,
    surface_identity,
)


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
    task = session.get(Task, admission.task_id)
    if not task_day_terminal_expired(admission) or not _task_day_recheck_allowed(task, admission):
        return False
    return _restart_admission_observation(
        session,
        admission,
        outcome="admission_recheck_on_new_task_day",
    )


def _task_day_recheck_allowed(task: Task | None, admission: TaskGroupBotAdmission) -> bool:
    return bool(
        task and task.status == "running" and task.deleted_at is None
        and task.retired_at is None and task.tenant_id == admission.tenant_id
        and int(task.task_lifecycle_epoch or 1) == int(admission.task_lifecycle_epoch or 1)
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
            _recoverable_terminal_predicate(task),
            TgAccount.deleted_at.is_(None),
            TgAccount.telegram_frozen.is_(False),
            TgAccount.status == AccountStatus.ACTIVE.value,
            TgAccount.session_ciphertext.is_not(None),
            TgAccount.session_ciphertext != "",
        )
        .order_by(TaskGroupBotAdmission.account_id)
        .limit(max(1, limit))
    ))
    reopened_ids = {
        row.account_id for row in admissions
        if _restart_recoverable_admission(session, row)
    }
    if not reopened_ids:
        return 0
    _reopen_coverages(session, task, group, reopened_ids)
    return len(reopened_ids)


def _recoverable_terminal_predicate(task: Task):
    return or_(
        TaskGroupBotAdmission.terminal_reason == UNPROVEN_TERMINAL_REASON,
        and_(
            TaskGroupBotAdmission.terminal_reason.in_(TASK_DAY_TERMINAL_REASONS),
            TaskGroupBotAdmission.task_lifecycle_epoch == int(task.task_lifecycle_epoch or 1),
            TaskGroupBotAdmission.terminal_evidence["terminal_date"].as_string() < _now().date().isoformat(),
        ),
    )


def _restart_recoverable_admission(session: Session, admission: TaskGroupBotAdmission) -> bool:
    if admission.terminal_reason == UNPROVEN_TERMINAL_REASON:
        return restart_unproven_admission(session, admission)
    return restart_task_day_admission(session, admission)


def _account_can_be_observed(account: TgAccount | None) -> bool:
    return bool(
        account
        and account.deleted_at is None
        and not account.telegram_frozen
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
        "consecutive_observation_gaps": 0,
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
        TaskAccountDailyCoverage.blocker_code.in_((
            "account_task_abandoned", "c2_observation_evidence_missing", "target_entity_unresolvable",
        )),
        TaskAccountDailyCoverage.reserved_action_id.is_(None),
        TaskAccountDailyCoverage.reservation_token.is_(None),
    ))
    for row in rows:
        row.state = "pending_admission"
        row.blocker_code = "group_bot_admission_wait"
        row.blocker_stage = "admission"
        row.blocker_detail = "当前任务日已重新检查准入恢复条件，重开账号观察"
        row.recovery_path = "task_group_bot_observation"
        row.next_eligible_at = _now()


__all__ = [
    "reopen_unproven_task_coverages",
    "restart_task_day_admission",
    "restart_stale_confirmation_observation",
    "restart_unproven_admission",
]

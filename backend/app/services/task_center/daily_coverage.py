from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models import (
    AccountStatus,
    Action,
    ExecutionAttempt,
    Task,
    TaskAccountDailyCoverage,
    TaskMembershipAdmissionItem,
    TgAccount,
    TgGroup,
    TgGroupAccount,
)
from app.services._common import _now

from .daily_coverage_schedule import daily_coverage_due_debt
from .daily_coverage_readiness import refresh_rows
from .daily_coverage_planning import advance_coverage_plan_cursor, ready_coverage_plan_batch
from .targets import group_from_reference


TERMINAL_PRECONFIRMATION_STATUSES = frozenset({"failed", "skipped", "retryable_failed"})
RECOVERABLE_TERMINAL_COVERAGE_STATES = frozenset({"reserved", "sending", "unknown"})
GENERATION_CONTRACT_ERROR_CODES = frozenset({
    "generation_contract_error",
    "ai_generation_output_count_mismatch",
    "ai_generation_slot_mapping_invalid",
    "ai_generation_slot_mapping_mismatch",
    "ai_generation_output_sequence_duplicate",
    "ai_generation_output_sequence_mismatch",
    "ai_generation_reply_sequence_mismatch",
    "ai_generation_reply_sequence_unexpected",
    "ai_generation_output_empty",
})
VOICE_PROFILE_MISSING_BLOCKER_CODE = "voice_profile_missing"
VOICE_PROFILE_MISSING_MESSAGE = "账号面具待恢复，受影响账号已隔离并等待自动重建"


@dataclass(frozen=True)
class DailyCoverageSyncResult:
    coverage_date: date
    created: int
    refreshed: int


def ensure_task_daily_coverage(
    session: Session,
    task: Task,
    *,
    now: datetime | None = None,
    account_ids: list[int] | None = None,
    incremental: bool = False,
    target_group: TgGroup | None = None,
    refresh_existing: bool = False,
) -> DailyCoverageSyncResult:
    timestamp = now or _now()
    if account_ids is None and not incremental:
        release_terminal_coverage_reservations(session, task, timestamp.date())
        if not refresh_existing:
            return DailyCoverageSyncResult(
                coverage_date=timestamp.date(),
                created=0,
                refreshed=0,
            )
    items = _scope_items(session, task, account_ids)
    if not items:
        return DailyCoverageSyncResult(coverage_date=timestamp.date(), created=0, refreshed=0)
    group = target_group or _task_group(session, task)
    if group.tenant_id != task.tenant_id:
        raise ValueError("all-account coverage task target group tenant mismatch")
    coverage_date = _target_date(group, timestamp, incremental=incremental)
    existing = _existing_rows(session, task, group.id, coverage_date, [item.account_id for item in items])
    created = 0
    rows_and_items: list[tuple[TaskAccountDailyCoverage, TaskMembershipAdmissionItem]] = []
    for item in items:
        row = existing.get(item.account_id)
        if row is None:
            row = _new_coverage(task, group, item, coverage_date, timestamp)
            session.add(row)
            existing[item.account_id] = row
            created += 1
        rows_and_items.append((row, item))
    refresh_rows(session, rows_and_items, group, timestamp)
    session.flush()
    return DailyCoverageSyncResult(coverage_date=coverage_date, created=created, refreshed=len(items))


def release_terminal_coverage_reservations(session: Session, task: Task, coverage_date: date) -> int:
    rows = list(session.execute(
        select(TaskAccountDailyCoverage, Action)
        .join(Action, Action.id == TaskAccountDailyCoverage.reserved_action_id)
        .where(
            TaskAccountDailyCoverage.task_id == task.id,
            TaskAccountDailyCoverage.coverage_date == coverage_date,
            TaskAccountDailyCoverage.state.in_(RECOVERABLE_TERMINAL_COVERAGE_STATES),
            Action.status.in_(TERMINAL_PRECONFIRMATION_STATUSES),
        )
    ))
    return _release_terminal_rows(session, rows)


def recover_terminal_coverage_reservations(
    session: Session,
    *,
    limit: int = 100,
    now: datetime | None = None,
) -> int:
    coverage_date = (now or _now()).date()
    rows = list(session.execute(
        select(TaskAccountDailyCoverage, Action)
        .join(Action, Action.id == TaskAccountDailyCoverage.reserved_action_id)
        .where(
            TaskAccountDailyCoverage.coverage_date == coverage_date,
            TaskAccountDailyCoverage.state.in_(RECOVERABLE_TERMINAL_COVERAGE_STATES),
            Action.status.in_(TERMINAL_PRECONFIRMATION_STATUSES),
        )
        .order_by(TaskAccountDailyCoverage.updated_at.asc(), TaskAccountDailyCoverage.id.asc())
        .limit(max(1, int(limit)))
        .with_for_update(skip_locked=True)
    ))
    return _release_terminal_rows(session, rows)


def _release_terminal_rows(session: Session, rows) -> int:
    released = 0
    gateway_started_ids = _gateway_started_action_ids(session, rows)
    for coverage, action in rows:
        if coverage.state == "unknown" and action.id in gateway_started_ids:
            continue
        result = action.result if isinstance(action.result, dict) else {}
        code = str(result.get("error_code") or action.status)
        detail = str(result.get("error_message") or "")
        if code in GENERATION_CONTRACT_ERROR_CODES:
            released += int(block_generation_contract_coverage(session, coverage.id, action.id, blocker_code=code, blocker_detail=detail))
        else:
            released += int(release_coverage_reservation(session, coverage.id, action.id, blocker_code=code, blocker_detail=detail))
    if released:
        session.flush()
    return released


def _gateway_started_action_ids(session: Session, rows) -> set[str]:
    action_ids = [action.id for coverage, action in rows if coverage.state == "unknown"]
    if not action_ids:
        return set()
    return set(session.scalars(
        select(ExecutionAttempt.action_id).where(
            ExecutionAttempt.action_id.in_(action_ids),
            ExecutionAttempt.gateway_call_started_at.is_not(None),
        ).distinct()
    ))


def reserve_coverage_for_action(
    session: Session,
    coverage_id: str,
    action_id: str,
    *,
    now: datetime | None = None,
) -> bool:
    result = session.execute(
        update(TaskAccountDailyCoverage)
        .where(
            TaskAccountDailyCoverage.id == coverage_id,
            TaskAccountDailyCoverage.state == "ready",
            TaskAccountDailyCoverage.reserved_action_id.is_(None),
            TaskAccountDailyCoverage.confirmed_count < TaskAccountDailyCoverage.target_count,
        )
        .values(
            state="reserved",
            reserved_action_id=action_id,
            reservation_token=None,
            last_action_id=action_id,
            blocker_code="",
            blocker_stage="",
            blocker_detail="",
            recovery_path="",
            next_decision_at=None,
            updated_at=now or _now(),
        )
    )
    return result.rowcount == 1


def reserve_coverage_for_planned_action(
    session: Session,
    coverage_id: str,
    reservation_token: str,
    *,
    now: datetime | None = None,
    allowed_states: tuple[str, ...] = ("ready",),
) -> bool:
    result = session.execute(
        update(TaskAccountDailyCoverage)
        .where(
            TaskAccountDailyCoverage.id == coverage_id,
            TaskAccountDailyCoverage.state.in_(allowed_states),
            TaskAccountDailyCoverage.reserved_action_id.is_(None),
            TaskAccountDailyCoverage.reservation_token.is_(None),
            TaskAccountDailyCoverage.confirmed_count < TaskAccountDailyCoverage.target_count,
        )
        .values(
            state="reserved",
            reservation_token=reservation_token,
            last_action_id=None,
            blocker_code="",
            blocker_stage="",
            blocker_detail="",
            recovery_path="",
            next_decision_at=None,
            updated_at=now or _now(),
        )
    )
    return result.rowcount == 1


def bind_coverage_reservation(
    session: Session,
    coverage_id: str,
    reservation_token: str,
    action_id: str,
) -> bool:
    result = session.execute(
        update(TaskAccountDailyCoverage)
        .where(
            TaskAccountDailyCoverage.id == coverage_id,
            TaskAccountDailyCoverage.state == "reserved",
            TaskAccountDailyCoverage.reserved_action_id.is_(None),
            TaskAccountDailyCoverage.reservation_token == reservation_token,
        )
        .values(
            reserved_action_id=action_id,
            reservation_token=None,
            last_action_id=action_id,
            updated_at=_now(),
        )
    )
    return result.rowcount == 1


def release_planned_coverage_reservation(
    session: Session,
    coverage_id: str,
    reservation_token: str,
    *,
    blocker_code: str,
    blocker_detail: str = "",
) -> bool:
    blocker_stage, recovery_path = _recovery_for_blocker(blocker_code)
    result = session.execute(
        update(TaskAccountDailyCoverage)
        .where(
            TaskAccountDailyCoverage.id == coverage_id,
            TaskAccountDailyCoverage.state == "reserved",
            TaskAccountDailyCoverage.reserved_action_id.is_(None),
            TaskAccountDailyCoverage.reservation_token == reservation_token,
        )
        .values(
            state="ready",
            reservation_token=None,
            last_action_id=None,
            blocker_code=blocker_code,
            blocker_stage=blocker_stage,
            blocker_detail=blocker_detail,
            recovery_path=recovery_path,
            next_eligible_at=None,
            next_decision_at=None,
            updated_at=_now(),
        )
    )
    return result.rowcount == 1


def release_coverage_reservation(
    session: Session,
    coverage_id: str,
    action_id: str,
    *,
    blocker_code: str,
    blocker_detail: str = "",
    next_eligible_at: datetime | None = None,
) -> bool:
    blocker_stage, recovery_path = _recovery_for_blocker(blocker_code)
    result = session.execute(
        update(TaskAccountDailyCoverage)
        .where(
            TaskAccountDailyCoverage.id == coverage_id,
            TaskAccountDailyCoverage.reserved_action_id == action_id,
            TaskAccountDailyCoverage.state.in_(("reserved", "sending", "unknown")),
        )
        .values(
            state="ready",
            reserved_action_id=None,
            reservation_token=None,
            last_action_id=action_id,
            blocker_code=blocker_code,
            blocker_stage=blocker_stage,
            blocker_detail=blocker_detail,
            recovery_path=recovery_path,
            next_eligible_at=next_eligible_at,
            next_decision_at=next_eligible_at,
            updated_at=_now(),
        )
    )
    return result.rowcount == 1


def block_generation_contract_coverage(
    session: Session,
    coverage_id: str,
    action_id: str,
    *,
    blocker_code: str,
    blocker_detail: str,
) -> bool:
    result = session.execute(
        update(TaskAccountDailyCoverage)
        .where(
            TaskAccountDailyCoverage.id == coverage_id,
            TaskAccountDailyCoverage.reserved_action_id == action_id,
            TaskAccountDailyCoverage.state.in_(("reserved", "sending", "unknown")),
        )
        .values(
            state="blocked",
            reserved_action_id=None,
            reservation_token=None,
            last_action_id=action_id,
            blocker_code=blocker_code,
            blocker_stage="generation_contract",
            blocker_detail=blocker_detail,
            recovery_path="generation_contract_repair",
            next_eligible_at=None,
            next_decision_at=None,
            updated_at=_now(),
        )
    )
    return result.rowcount == 1


def confirm_coverage_from_attempt(
    session: Session,
    coverage_id: str,
    action_id: str,
    attempt: ExecutionAttempt | None,
) -> bool:
    if attempt is None or attempt.status != "success" or not str(attempt.remote_message_id or "").strip():
        return False
    row = session.scalar(
        select(TaskAccountDailyCoverage)
        .where(TaskAccountDailyCoverage.id == coverage_id)
        .with_for_update()
    )
    if row is None or row.reserved_action_id != action_id:
        return False
    if attempt.account_id is None or int(attempt.account_id) != int(row.account_id):
        return False
    if row.last_success_action_id == action_id:
        return True
    row.confirmed_count = min(row.target_count, row.confirmed_count + 1)
    row.last_success_action_id = action_id
    row.last_action_id = action_id
    row.last_remote_message_id = str(attempt.remote_message_id)
    row.reserved_action_id = None
    row.reservation_token = None
    row.blocker_code = ""
    row.blocker_stage = ""
    row.blocker_detail = ""
    row.recovery_path = ""
    row.next_decision_at = None
    row.updated_at = _now()
    if row.confirmed_count >= row.target_count:
        row.state = "confirmed"
        row.completed_at = _now()
    else:
        row.state = "ready"
    return True


def mark_coverage_unknown(
    session: Session,
    coverage_id: str,
    action_id: str,
    *,
    blocker_code: str,
    blocker_detail: str,
) -> bool:
    result = session.execute(
        update(TaskAccountDailyCoverage)
        .where(
            TaskAccountDailyCoverage.id == coverage_id,
            TaskAccountDailyCoverage.reserved_action_id == action_id,
            TaskAccountDailyCoverage.state.in_(("reserved", "sending", "unknown")),
        )
        .values(
            state="unknown",
            reservation_token=None,
            last_action_id=action_id,
            blocker_code=blocker_code,
            blocker_stage="remote_reconcile",
            blocker_detail=blocker_detail,
            recovery_path="remote_reconcile",
            next_decision_at=None,
            updated_at=_now(),
        )
    )
    return result.rowcount == 1


def block_coverage_accounts(
    session: Session,
    task: Task,
    account_ids: list[int],
    *,
    blocker_code: str,
    blocker_detail: str,
    next_eligible_at: datetime,
) -> int:
    if not account_ids:
        return 0
    result = session.execute(
        update(TaskAccountDailyCoverage)
        .where(
            TaskAccountDailyCoverage.task_id == task.id,
            TaskAccountDailyCoverage.coverage_date == _now().date(),
            TaskAccountDailyCoverage.account_id.in_(account_ids),
            TaskAccountDailyCoverage.confirmed_count < TaskAccountDailyCoverage.target_count,
            TaskAccountDailyCoverage.state.in_(("ready", "blocked")),
        )
        .values(
            state="blocked",
            blocker_code=blocker_code,
            blocker_stage="admission",
            blocker_detail=blocker_detail,
            recovery_path="permission_recheck",
            next_eligible_at=next_eligible_at,
            next_decision_at=next_eligible_at,
            updated_at=_now(),
        )
    )
    return int(result.rowcount or 0)


def block_voice_profile_coverage(
    session: Session,
    *,
    task: Task,
    account_ids: list[int],
    next_retry_at: datetime | None,
    detail: str,
    now: datetime | None = None,
) -> int:
    if not account_ids:
        return 0
    timestamp = now or _now()
    result = session.execute(
        update(TaskAccountDailyCoverage)
        .where(
            TaskAccountDailyCoverage.task_id == task.id,
            TaskAccountDailyCoverage.coverage_date == timestamp.date(),
            TaskAccountDailyCoverage.account_id.in_(account_ids),
            TaskAccountDailyCoverage.confirmed_count < TaskAccountDailyCoverage.target_count,
            TaskAccountDailyCoverage.state.in_(("ready", "blocked")),
        )
        .values(
            state="blocked",
            blocker_code=VOICE_PROFILE_MISSING_BLOCKER_CODE,
            blocker_stage="voice_profile",
            blocker_detail=detail,
            recovery_path="voice_profile_generation",
            next_eligible_at=next_retry_at,
            next_decision_at=next_retry_at,
            updated_at=timestamp,
        )
    )
    return int(result.rowcount or 0)


def release_voice_profile_coverage_for_check_in(
    session: Session,
    task: Task,
    *,
    now: datetime | None = None,
) -> int:
    timestamp = now or _now()
    result = session.execute(
        update(TaskAccountDailyCoverage)
        .where(
            TaskAccountDailyCoverage.task_id == task.id,
            TaskAccountDailyCoverage.coverage_date == timestamp.date(),
            TaskAccountDailyCoverage.state == "blocked",
            TaskAccountDailyCoverage.blocker_code == VOICE_PROFILE_MISSING_BLOCKER_CODE,
            TaskAccountDailyCoverage.confirmed_count < TaskAccountDailyCoverage.target_count,
        )
        .values(
            state="ready",
            blocker_code="",
            blocker_stage="",
            blocker_detail="",
            recovery_path="mask_missing_check_in",
            next_eligible_at=timestamp,
            next_decision_at=timestamp,
            updated_at=timestamp,
        )
    )
    return int(result.rowcount or 0)


def release_voice_profile_coverage(
    session: Session,
    *,
    tenant_id: int,
    account_id: int,
    now: datetime | None = None,
) -> int:
    timestamp = now or _now()
    task_ids = _voice_profile_blocked_task_ids(session, tenant_id, account_id, timestamp)
    if not task_ids:
        return 0
    result = session.execute(
        update(TaskAccountDailyCoverage)
        .where(
            TaskAccountDailyCoverage.tenant_id == tenant_id,
            TaskAccountDailyCoverage.task_id.in_(task_ids),
            TaskAccountDailyCoverage.account_id == account_id,
            TaskAccountDailyCoverage.coverage_date == timestamp.date(),
            TaskAccountDailyCoverage.state == "blocked",
            TaskAccountDailyCoverage.blocker_code == VOICE_PROFILE_MISSING_BLOCKER_CODE,
            _sendable_coverage_account(tenant_id, account_id),
        )
        .values(
            state="ready",
            blocker_code="",
            blocker_stage="",
            blocker_detail="",
            recovery_path="",
            next_eligible_at=None,
            next_decision_at=timestamp,
            targeted_at=timestamp,
            updated_at=timestamp,
        )
    )
    released = int(result.rowcount or 0)
    if released:
        _refresh_voice_profile_task_stats(session, task_ids, timestamp)
        session.execute(
            update(Task)
            .where(Task.id.in_(task_ids), Task.status == "running")
            .values(next_run_at=timestamp, updated_at=timestamp)
        )
    return released


def _voice_profile_blocked_task_ids(
    session: Session,
    tenant_id: int,
    account_id: int,
    timestamp: datetime,
) -> list[str]:
    return list(session.scalars(
        select(TaskAccountDailyCoverage.task_id).where(
            TaskAccountDailyCoverage.tenant_id == tenant_id,
            TaskAccountDailyCoverage.account_id == account_id,
            TaskAccountDailyCoverage.coverage_date == timestamp.date(),
            TaskAccountDailyCoverage.state == "blocked",
            TaskAccountDailyCoverage.blocker_code == VOICE_PROFILE_MISSING_BLOCKER_CODE,
        )
    ))


def _refresh_voice_profile_task_stats(session: Session, task_ids: list[str], timestamp: datetime) -> None:
    for task in session.scalars(select(Task).where(Task.id.in_(task_ids))):
        missing_count = session.scalar(
            select(func.count(func.distinct(TaskAccountDailyCoverage.account_id))).where(
                TaskAccountDailyCoverage.task_id == task.id,
                TaskAccountDailyCoverage.coverage_date == timestamp.date(),
                TaskAccountDailyCoverage.state == "blocked",
                TaskAccountDailyCoverage.blocker_code == VOICE_PROFILE_MISSING_BLOCKER_CODE,
            )
        )
        stats = dict(task.stats or {})
        stats["voice_profile_missing_account_count"] = int(missing_count or 0)
        task.stats = stats
        if not missing_count and task.last_error == VOICE_PROFILE_MISSING_MESSAGE:
            task.last_error = ""


def _sendable_coverage_account(tenant_id: int, account_id: int):
    return select(TgGroupAccount.id).join(
        TgAccount,
        TgAccount.id == TgGroupAccount.account_id,
    ).where(
        TgGroupAccount.tenant_id == tenant_id,
        TgGroupAccount.group_id == TaskAccountDailyCoverage.group_id,
        TgGroupAccount.account_id == account_id,
        TgGroupAccount.can_send.is_(True),
        TgAccount.tenant_id == tenant_id,
        TgAccount.deleted_at.is_(None),
        TgAccount.status == AccountStatus.ACTIVE.value,
        TgAccount.session_ciphertext.is_not(None),
        TgAccount.session_ciphertext != "",
    ).exists()


def release_online_coverage_blockers(
    session: Session,
    *,
    tenant_id: int,
    account_id: int,
    now: datetime | None = None,
) -> int:
    timestamp = now or _now()
    sendable_membership = select(TgGroupAccount.id).where(
        TgGroupAccount.tenant_id == tenant_id,
        TgGroupAccount.group_id == TaskAccountDailyCoverage.group_id,
        TgGroupAccount.account_id == account_id,
        TgGroupAccount.can_send.is_(True),
    ).exists()
    result = session.execute(
        update(TaskAccountDailyCoverage)
        .where(
            TaskAccountDailyCoverage.tenant_id == tenant_id,
            TaskAccountDailyCoverage.account_id == account_id,
            TaskAccountDailyCoverage.coverage_date == timestamp.date(),
            TaskAccountDailyCoverage.state == "blocked",
            TaskAccountDailyCoverage.blocker_code == "account_offline",
            TaskAccountDailyCoverage.confirmed_count < TaskAccountDailyCoverage.target_count,
            sendable_membership,
        )
        .values(
            state="ready",
            blocker_code="",
            blocker_stage="",
            blocker_detail="",
            recovery_path="",
            next_eligible_at=None,
            next_decision_at=timestamp,
            targeted_at=timestamp,
            updated_at=timestamp,
        )
    )
    return int(result.rowcount or 0)


def release_generation_contract_blocker(
    session: Session,
    coverage_id: str,
    *,
    approved_reason: str,
    now: datetime | None = None,
) -> bool:
    if not str(approved_reason or "").strip():
        raise ValueError("generation contract recovery requires an approval reason")
    timestamp = now or _now()
    result = session.execute(
        update(TaskAccountDailyCoverage)
        .where(
            TaskAccountDailyCoverage.id == coverage_id,
            TaskAccountDailyCoverage.state == "blocked",
            TaskAccountDailyCoverage.blocker_stage == "generation_contract",
        )
        .values(
            state="ready",
            blocker_code="",
            blocker_stage="",
            blocker_detail=approved_reason,
            recovery_path="",
            next_eligible_at=None,
            next_decision_at=timestamp,
            targeted_at=timestamp,
            updated_at=timestamp,
        )
    )
    return result.rowcount == 1


def _recovery_for_blocker(blocker_code: str) -> tuple[str, str]:
    if blocker_code in {"duplicate_message", "content_variation_key_conflict"}:
        return "quality", "replan_with_new_variation"
    if blocker_code in GENERATION_CONTRACT_ERROR_CODES:
        return "generation_contract", "generation_contract_repair"
    return "planning", ""


def backfill_daily_coverage_confirmations(
    session: Session,
    task: Task,
    coverage_date: date,
) -> int:
    rows = list(session.scalars(select(TaskAccountDailyCoverage).where(
        TaskAccountDailyCoverage.task_id == task.id,
        TaskAccountDailyCoverage.coverage_date == coverage_date,
    )))
    if not rows:
        return 0
    start = datetime.combine(coverage_date, datetime.min.time())
    end = start + timedelta(days=1)
    attempts = session.execute(
        select(
            Action.id,
            Action.account_id,
            Action.executed_at,
            ExecutionAttempt.remote_message_id,
        )
        .join(ExecutionAttempt, ExecutionAttempt.action_id == Action.id)
        .where(
            Action.task_id == task.id,
            Action.action_type == "send_message",
            Action.status == "success",
            Action.executed_at >= start,
            Action.executed_at < end,
            ExecutionAttempt.status == "success",
            ExecutionAttempt.remote_message_id != "",
        )
        .order_by(Action.executed_at.asc(), ExecutionAttempt.attempt_no.asc())
    )
    successes = _successes_by_account(attempts)
    return sum(_apply_backfilled_successes(row, successes.get(row.account_id, [])) for row in rows)


def _successes_by_account(attempts) -> dict[int, list[tuple[str, datetime, str]]]:
    grouped: dict[int, dict[str, tuple[str, datetime, str]]] = {}
    for action_id, account_id, executed_at, remote_message_id in attempts:
        if account_id is None or executed_at is None or not str(remote_message_id or "").strip():
            continue
        grouped.setdefault(int(account_id), {})[str(action_id)] = (
            str(action_id), executed_at, str(remote_message_id),
        )
    return {account_id: list(actions.values()) for account_id, actions in grouped.items()}


def _apply_backfilled_successes(
    row: TaskAccountDailyCoverage,
    successes: list[tuple[str, datetime, str]],
) -> int:
    if not successes:
        return 0
    observed = min(row.target_count, len(successes))
    if observed < row.confirmed_count:
        return 0
    if observed == row.confirmed_count and row.last_success_action_id:
        return 0
    confirmed = max(row.confirmed_count, observed)
    action_id, executed_at, remote_message_id = successes[-1]
    changed = row.confirmed_count != confirmed or row.last_success_action_id != action_id
    row.confirmed_count = confirmed
    row.last_success_action_id = action_id
    row.last_remote_message_id = remote_message_id
    if confirmed >= row.target_count:
        row.state = "confirmed"
        row.completed_at = executed_at
        row.reserved_action_id = None
        row.reservation_token = None
        row.blocker_code = ""
        row.blocker_detail = ""
    row.updated_at = _now()
    return int(changed)


def ready_coverage_rows(
    session: Session,
    task: Task,
    *,
    now: datetime | None = None,
    limit: int | None = None,
) -> list[TaskAccountDailyCoverage]:
    timestamp = now or _now()
    stmt = _ready_coverage_stmt(task, timestamp)
    if limit is not None:
        stmt = stmt.limit(max(1, int(limit)))
    return list(session.scalars(stmt))


def ready_coverage_rows_by_account(
    session: Session,
    task: Task,
    account_ids: list[int],
    *,
    now: datetime | None = None,
) -> dict[int, TaskAccountDailyCoverage]:
    wanted = set(account_ids)
    if not wanted:
        return {}
    return {
        row.account_id: row
        for row in ready_coverage_rows(session, task, now=now)
        if row.account_id in wanted
    }


def ready_coverage_remaining_count(session: Session, task: Task, *, now: datetime | None = None) -> int:
    return sum(max(0, row.target_count - row.confirmed_count) for row in ready_coverage_rows(session, task, now=now))


def _ready_coverage_stmt(task: Task, timestamp: datetime):
    return (
        select(TaskAccountDailyCoverage)
        .where(
            TaskAccountDailyCoverage.task_id == task.id,
            TaskAccountDailyCoverage.coverage_date == timestamp.date(),
            TaskAccountDailyCoverage.state == "ready",
            TaskAccountDailyCoverage.confirmed_count < TaskAccountDailyCoverage.target_count,
            (
                TaskAccountDailyCoverage.next_eligible_at.is_(None)
                | (TaskAccountDailyCoverage.next_eligible_at <= timestamp)
            ),
        )
        .order_by(
            TaskAccountDailyCoverage.next_eligible_at.asc().nullsfirst(),
            TaskAccountDailyCoverage.targeted_at.asc(),
            TaskAccountDailyCoverage.account_id.asc(),
        )
    )


def _task_group(session: Session, task: Task) -> TgGroup:
    config = task.type_config or {}
    group_id = int(config.get("target_group_id") or 0)
    group = session.get(TgGroup, group_id) if group_id else None
    if group is None or group.tenant_id != task.tenant_id:
        group = group_from_reference(
            session,
            task.tenant_id,
            operation_target_id=int(config.get("target_operation_target_id") or 0) or None,
            require_authorized=False,
        )
    if group is None:
        raise ValueError("all-account coverage task target group not found")
    return group


def _scope_items(session: Session, task: Task, account_ids: list[int] | None) -> list[TaskMembershipAdmissionItem]:
    stmt = select(TaskMembershipAdmissionItem).where(TaskMembershipAdmissionItem.task_id == task.id)
    if account_ids is not None:
        stmt = stmt.where(TaskMembershipAdmissionItem.account_id.in_(account_ids))
    return list(session.scalars(stmt.order_by(TaskMembershipAdmissionItem.account_id.asc())))


def _existing_rows(
    session: Session,
    task: Task,
    group_id: int,
    coverage_date: date,
    account_ids: list[int],
) -> dict[int, TaskAccountDailyCoverage]:
    if not account_ids:
        return {}
    rows = session.scalars(
        select(TaskAccountDailyCoverage).where(
            TaskAccountDailyCoverage.task_id == task.id,
            TaskAccountDailyCoverage.group_id == group_id,
            TaskAccountDailyCoverage.coverage_date == coverage_date,
            TaskAccountDailyCoverage.account_id.in_(account_ids),
        )
    )
    return {row.account_id: row for row in rows}


def _new_coverage(
    task: Task,
    group: TgGroup,
    item: TaskMembershipAdmissionItem,
    coverage_date: date,
    timestamp: datetime,
) -> TaskAccountDailyCoverage:
    return TaskAccountDailyCoverage(
        tenant_id=task.tenant_id,
        task_id=task.id,
        group_id=group.id,
        account_id=item.account_id,
        membership_item_id=item.id,
        coverage_date=coverage_date,
        target_count=1,
        targeted_at=timestamp,
    )


def _target_date(group: TgGroup, timestamp: datetime, *, incremental: bool) -> date:
    if not incremental:
        return timestamp.date()
    end_hour, end_minute = _window_end(group.active_window)
    end = timestamp.replace(hour=end_hour, minute=end_minute, second=0, microsecond=0)
    return timestamp.date() + timedelta(days=1) if timestamp >= end else timestamp.date()


def _window_end(active_window: str) -> tuple[int, int]:
    try:
        end_raw = str(active_window or "09:00-23:00").split("-", 1)[1]
        hour, minute = end_raw.split(":", 1)
        return int(hour), int(minute)
    except (IndexError, TypeError, ValueError):
        raise ValueError(f"invalid group active window: {active_window}")


__all__ = [
    "backfill_daily_coverage_confirmations",
    "bind_coverage_reservation",
    "block_generation_contract_coverage",
    "block_voice_profile_coverage",
    "block_coverage_accounts",
    "DailyCoverageSyncResult",
    "confirm_coverage_from_attempt",
    "daily_coverage_due_debt",
    "ensure_task_daily_coverage",
    "advance_coverage_plan_cursor",
    "ready_coverage_plan_batch",
    "ready_coverage_remaining_count",
    "ready_coverage_rows",
    "ready_coverage_rows_by_account",
    "mark_coverage_unknown",
    "recover_terminal_coverage_reservations",
    "release_online_coverage_blockers",
    "release_voice_profile_coverage",
    "release_voice_profile_coverage_for_check_in",
    "release_generation_contract_blocker",
    "release_coverage_reservation",
    "release_planned_coverage_reservation",
    "release_terminal_coverage_reservations",
    "reserve_coverage_for_action",
    "reserve_coverage_for_planned_action",
]

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models import Action, ConsistencyQuarantine, SearchClickFulfillmentObligation, SearchClickOpportunityAssignment, Task, TaskDayLedger
from app.timezone import beijing_now

from .search_join_facts import (
    search_join_fact_in_window,
    search_join_held_in_window,
)
from .search_click_action_progress import (
    confirmed_action_count as _confirmed_action_count,
    held_action_count as _held_action_count,
)


ACTION_TYPE_BY_TASK_TYPE = {
    "search_click": "search_join",
    "search_join_group": "search_join",
    "search_rank_deboost": "search_rank_deboost",
}
HELD_ACTION_STATUSES = (
    "pending",
    "claiming",
    "executing",
    "unknown_after_send",
)
TERMINAL_TASK_STATUSES = {"stopped", "failed", "deleted"}
SOURCE_TIMEZONE = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class SearchClickTargetProgress:
    target_count: int | None
    confirmed_count: int
    held_count: int
    remaining_slot_count: int | None
    scope: str = "lifecycle"
    local_date: str | None = None
    planning_click_deficit: int | None = None
    projected_eligible_attempt_capacity: int | None = None
    projection_not_reserved: bool = True
    committed_attempt_count: int = 0
    hard_safe_attempt_capacity: int | None = None
    catch_up_required: bool = False
    unknown_count: int = 0
    terminal_shortfall: int = 0
    active_quarantine_count: int = 0
    detailed_fulfillment: bool = False

    @property
    def is_daily_target(self) -> bool:
        return self.scope == "daily"

    @property
    def completed(self) -> bool:
        return (
            not self.is_daily_target
            and self.target_count is not None
            and self.confirmed_count >= self.target_count
        )

    @property
    def state(self) -> str:
        if self.completed:
            return "completed"
        if self.target_count is None:
            return "legacy_unlimited"
        if self.is_daily_target and self.confirmed_count >= self.target_count:
            if self._has_unresolved_consistency():
                return "waiting_consistency"
            return "daily_target_met"
        return "waiting_confirmation" if self.held_count else "planning"

    def _has_unresolved_consistency(self) -> bool:
        return self.detailed_fulfillment and any((
            self.committed_attempt_count,
            self.unknown_count,
            self.terminal_shortfall,
            self.active_quarantine_count,
        ))

    def as_dict(self) -> dict[str, int | str | None]:
        progress: dict[str, int | str | bool | None] = {
            "target_count": self.target_count,
            "confirmed_count": self.confirmed_count,
            "held_count": self.held_count,
            "remaining_slot_count": self.remaining_slot_count,
            "state": self.state,
        }
        if self.detailed_fulfillment:
            progress.update(self._detailed_metrics())
        if self.is_daily_target:
            progress["scope"] = "daily"
            progress["local_date"] = self.local_date
        return progress

    def _detailed_metrics(self) -> dict[str, int | bool | None]:
        return {
            "planning_click_deficit": self.planning_click_deficit,
            "projected_eligible_attempt_capacity":
                self.projected_eligible_attempt_capacity,
            "projection_not_reserved": self.projection_not_reserved,
            "committed_attempt_count": self.committed_attempt_count,
            "hard_safe_attempt_capacity": self.hard_safe_attempt_capacity,
            "catch_up_required": self.catch_up_required,
            "unknown_count": self.unknown_count,
            "terminal_shortfall": self.terminal_shortfall,
            "active_quarantine_count": self.active_quarantine_count,
        }


def search_click_target_progress(
    session: Session,
    task: Task,
    *,
    now_value: datetime | None = None,
    coverage_date: date | None = None,
) -> SearchClickTargetProgress:
    if task.type == "search_click":
        return _pure_search_click_target_progress(
            session,
            task,
            now_value=now_value,
            coverage_date=coverage_date,
        )
    if task.type == "search_join_group":
        return _search_join_click_target_progress(
            session,
            task,
            now_value=now_value,
            coverage_date=coverage_date,
        )
    daily_target_count = _daily_target_count(task)
    action_type = _action_type(task)
    if daily_target_count is not None:
        start_at, end_at, local_date = _local_day_bounds(task, now_value or beijing_now())
        confirmed_count = _confirmed_action_count(
            session, task, action_type, start_at=start_at, end_at=end_at
        )
        held_count = _held_action_count(
            session, task, action_type, HELD_ACTION_STATUSES, start_at=start_at, end_at=end_at
        )
        remaining = _remaining_slots(daily_target_count, confirmed_count, held_count)
        return SearchClickTargetProgress(
            daily_target_count,
            confirmed_count,
            held_count,
            remaining,
            "daily",
            local_date,
        )
    target_count = _target_count(task)
    confirmed_count = _confirmed_action_count(session, task, action_type)
    held_count = _held_action_count(session, task, action_type, HELD_ACTION_STATUSES)
    remaining = _remaining_slots(target_count, confirmed_count, held_count)
    return SearchClickTargetProgress(target_count, confirmed_count, held_count, remaining)


def _pure_search_click_target_progress(
    session: Session, task: Task, *, now_value: datetime | None, coverage_date: date | None,
) -> SearchClickTargetProgress:
    timestamp = now_value or beijing_now()
    _, _, local_date = _local_day_bounds(task, timestamp, coverage_date)
    ledger = session.scalar(
        select(TaskDayLedger).where(
            TaskDayLedger.task_id == task.id,
            TaskDayLedger.obligation_local_date == date.fromisoformat(local_date),
        )
    )
    target_count = _daily_click_target_count(task)
    if ledger is None:
        return SearchClickTargetProgress(target_count, 0, 0, target_count, "daily", local_date)
    rows = _click_obligations(session, ledger.id)
    confirmed = sum(
        row.status == "confirmed"
        and row.target_click_observed
        and bool(row.click_evidence_hash)
        for row in rows
    )
    held_statuses = {"assigned", "action_bound", "claiming", "executing"}
    held = sum(row.status in held_statuses for row in rows)
    unknown = sum(row.status == "unknown_after_send" for row in rows)
    committed = held + unknown
    remaining = max(0, target_count - confirmed)
    planning_deficit = max(0, remaining - committed)
    quarantine_count = _active_click_quarantine_count(session, rows)
    stats = task.stats or {}
    return SearchClickTargetProgress(
        target_count,
        confirmed,
        committed,
        remaining,
        "daily",
        local_date,
        planning_deficit,
        _optional_int(stats.get("projected_eligible_attempt_capacity")),
        True,
        committed,
        _optional_int(stats.get("hard_safe_attempt_capacity")),
        _catch_up_required(ledger, target_count, confirmed, committed, timestamp),
        unknown,
        sum(row.status == "terminal_shortfall" for row in rows),
        quarantine_count,
        True,
    )


def _click_obligations(
    session: Session,
    ledger_id: str,
) -> list[SearchClickFulfillmentObligation]:
    return list(session.scalars(
        select(SearchClickFulfillmentObligation).where(
            SearchClickFulfillmentObligation.task_day_ledger_id == ledger_id
        )
    ))


def search_join_membership_target_progress(
    session: Session,
    task: Task,
    *,
    now_value: datetime | None = None,
    coverage_date: date | None = None,
) -> SearchClickTargetProgress | None:
    if task.type != "search_join_group" or _daily_click_target_count(task) is None:
        return None
    daily_target_count = _daily_target_count(task)
    if daily_target_count is None:
        return None
    return _search_join_daily_progress(
        session,
        task,
        target_count=daily_target_count,
        now_value=now_value or beijing_now(),
        fact_kind="membership",
        coverage_date=coverage_date,
    )


def _search_join_click_target_progress(
    session: Session,
    task: Task,
    *,
    now_value: datetime | None,
    coverage_date: date | None,
) -> SearchClickTargetProgress:
    daily_click_target_count = _daily_click_target_count(task)
    if daily_click_target_count is not None:
        return _search_join_daily_progress(
            session,
            task,
            target_count=daily_click_target_count,
            now_value=now_value or beijing_now(),
            fact_kind="click",
            coverage_date=coverage_date,
        )
    daily_target_count = _daily_target_count(task)
    if daily_target_count is not None:
        return _search_join_daily_progress(
            session,
            task,
            target_count=daily_target_count,
            now_value=now_value or beijing_now(),
            fact_kind="membership",
            coverage_date=coverage_date,
        )
    action_type = _action_type(task)
    target_count = _target_count(task)
    confirmed_count = _confirmed_action_count(session, task, action_type)
    held_count = _held_action_count(session, task, action_type, HELD_ACTION_STATUSES)
    remaining = _remaining_slots(target_count, confirmed_count, held_count)
    return SearchClickTargetProgress(target_count, confirmed_count, held_count, remaining)


def _search_join_daily_progress(
    session: Session,
    task: Task,
    *,
    target_count: int,
    now_value: datetime,
    fact_kind: str,
    coverage_date: date | None,
) -> SearchClickTargetProgress:
    start_at, end_at, local_date = _local_day_bounds(task, now_value, coverage_date)
    actions = session.scalars(
        select(Action).where(
            Action.tenant_id == task.tenant_id,
            Action.task_id == task.id,
            Action.action_type == "search_join",
        )
    )
    rows = list(actions)
    confirmed_count = sum(
        search_join_fact_in_window(action, start_at, end_at, fact_kind)
        for action in rows
    )
    held_count = sum(
        search_join_held_in_window(action, start_at, end_at, fact_kind, statuses=HELD_ACTION_STATUSES)
        for action in rows
    )
    return SearchClickTargetProgress(
        target_count,
        confirmed_count,
        held_count,
        _remaining_slots(target_count, confirmed_count, held_count),
        "daily",
        local_date,
    )


def reconcile_search_click_target_progress(
    session: Session,
    task: Task,
    *,
    now_value: datetime | None = None,
) -> SearchClickTargetProgress:
    progress = search_click_target_progress(session, task, now_value=now_value)
    if progress.target_count is None:
        return progress
    stats = dict(task.stats or {})
    stats["search_click_target"] = progress.as_dict()
    membership_progress = search_join_membership_target_progress(
        session, task, now_value=now_value
    )
    if membership_progress is not None:
        stats["search_join_membership_target"] = membership_progress.as_dict()
    if progress.is_daily_target:
        if stats.get("completion_reason") == "target_count_reached":
            stats.pop("completion_reason")
        task.stats = stats
        return progress
    if progress.completed:
        stats["completion_reason"] = "target_count_reached"
        if task.status not in TERMINAL_TASK_STATUSES:
            task.status = "completed"
            task.next_run_at = None
    elif stats.get("completion_reason") == "target_count_reached":
        stats.pop("completion_reason")
    task.stats = stats
    return progress


def _target_count(task: Task) -> int | None:
    value = (task.type_config or {}).get("target_count")
    if value is None:
        return None
    try:
        target_count = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("search_click_target_count_invalid") from exc
    if target_count <= 0:
        raise ValueError("search_click_target_count_invalid")
    return target_count


def _daily_target_count(task: Task) -> int | None:
    if task.type != "search_join_group":
        return None
    value = (task.type_config or {}).get("daily_target_count")
    if value is None:
        return None
    try:
        target_count = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("search_click_daily_target_count_invalid") from exc
    if target_count <= 0:
        raise ValueError("search_click_daily_target_count_invalid")
    return target_count


def _daily_click_target_count(task: Task) -> int | None:
    if task.type not in {"search_click", "search_join_group"}:
        return None
    value = (task.type_config or {}).get("daily_click_target_count")
    if value is None:
        return None
    try:
        target_count = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("search_click_daily_click_target_count_invalid") from exc
    if target_count <= 0:
        raise ValueError("search_click_daily_click_target_count_invalid")
    return target_count


def _action_type(task: Task) -> str:
    action_type = ACTION_TYPE_BY_TASK_TYPE.get(task.type)
    if action_type is None:
        raise ValueError(f"search_click_target_type_unsupported:{task.type}")
    return action_type


def _local_day_bounds(
    task: Task,
    now_value: datetime,
    coverage_date: date | None = None,
) -> tuple[datetime, datetime, str]:
    source_now = now_value if now_value.tzinfo else now_value.replace(tzinfo=SOURCE_TIMEZONE)
    timezone = ZoneInfo(task.timezone or "Asia/Shanghai")
    local_now = source_now.astimezone(timezone)
    selected_date = coverage_date or local_now.date()
    local_start = datetime.combine(selected_date, time.min, tzinfo=timezone)
    return (
        _source_naive(local_start),
        _source_naive(local_start + timedelta(days=1)),
        local_now.date().isoformat(),
    )


def _source_naive(value: datetime) -> datetime:
    return value.astimezone(SOURCE_TIMEZONE).replace(tzinfo=None)


def _remaining_slots(target_count: int | None, confirmed_count: int, held_count: int) -> int | None:
    if target_count is None:
        return None
    return max(0, target_count - confirmed_count - held_count)


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    return int(value)


def _catch_up_required(
    ledger: TaskDayLedger,
    target_count: int,
    confirmed: int,
    committed: int,
    now_value: datetime,
) -> bool:
    start = _source_naive(ledger.period_start_at)
    deadline = _source_naive(ledger.deadline_at)
    current = _source_naive(now_value)
    total_seconds = max(1.0, (deadline - start).total_seconds())
    elapsed_seconds = min(
        total_seconds,
        max(0.0, (current - start).total_seconds()),
    )
    due_by_now = int(target_count * elapsed_seconds / total_seconds)
    return confirmed + committed < due_by_now


def _active_click_quarantine_count(
    session: Session,
    obligations: list[SearchClickFulfillmentObligation],
) -> int:
    obligation_ids = [row.id for row in obligations]
    if not obligation_ids:
        return 0
    unit_scope_ids = [
        f"{reservation_id}:{ordinal}"
        for reservation_id, ordinal in session.execute(
            select(
                SearchClickOpportunityAssignment.dispatch_claim_reservation_id,
                SearchClickOpportunityAssignment.fulfillment_lane_claim_ordinal,
            ).where(
                SearchClickOpportunityAssignment.obligation_id.in_(
                    obligation_ids
                )
            )
        )
    ]
    return int(session.scalar(
        select(func.count(ConsistencyQuarantine.id)).where(
            or_(
                (
                    ConsistencyQuarantine.scope_type
                    == "search_click_obligation"
                )
                & ConsistencyQuarantine.scope_id.in_(obligation_ids),
                (
                    ConsistencyQuarantine.scope_type
                    == "dispatch_reservation_unit"
                )
                & ConsistencyQuarantine.scope_id.in_(unit_scope_ids),
            ),
            ConsistencyQuarantine.status == "active",
        )
    ) or 0)


__all__ = [
    "SearchClickTargetProgress",
    "reconcile_search_click_target_progress",
    "search_click_target_progress",
    "search_join_membership_target_progress",
]

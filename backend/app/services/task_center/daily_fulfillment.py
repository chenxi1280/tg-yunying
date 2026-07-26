from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models import AiGenerationContractAudit, Action, Task, TaskAccountDailyCoverage, TaskDailyFulfillmentDecision
from app.services._common import _now

from .datetime_compat import is_after_or_equal, is_before


OPEN_ACTION_STATUSES = frozenset({"pending", "claiming", "executing"})
DECISION_RECHECK_SECONDS = 120


@dataclass(frozen=True)
class DailyFulfillmentSummary:
    frozen_denominator_count: int
    confirmed_count: int
    ready_count: int
    reserved_or_sending_count: int
    unknown_hold_count: int
    blocked_count: int
    full_shortfall_count: int
    valid_future_open_cover_count: int
    overdue_open_count: int
    ready_to_plan_count: int
    blocked_shortfall_count: int
    sendable_capacity_count: int
    blocker_counts: dict[str, int]
    daily_outcome: str
    next_decision_at: datetime | None

    def as_dict(self) -> dict:
        data = asdict(self)
        data["next_decision_at"] = self.next_decision_at.isoformat() if self.next_decision_at else None
        data["required_new"] = self.ready_to_plan_count
        return data


def summarize_daily_fulfillment(
    session: Session,
    task: Task,
    *,
    now: datetime | None = None,
    coverage_date: date | None = None,
) -> DailyFulfillmentSummary:
    timestamp = now or _now()
    rows = _current_rows(session, task, coverage_date or timestamp.date())
    _reconcile_open_coverage_rows(session, task, rows, timestamp)
    actions = _actions_by_id(session, rows)
    counts = _state_counts(rows, actions, timestamp)
    return _summary_from_counts(rows, counts, timestamp)


def record_daily_fulfillment_decision(
    session: Session,
    task: Task,
    *,
    reason: str,
    hard_hourly_required_new: int = 0,
    now: datetime | None = None,
) -> DailyFulfillmentSummary:
    timestamp = now or _now()
    summary = summarize_daily_fulfillment(session, task, now=timestamp)
    session.add(TaskDailyFulfillmentDecision(
        tenant_id=task.tenant_id,
        task_id=task.id,
        coverage_date=timestamp.date(),
        decided_at=timestamp,
        full_shortfall_count=summary.full_shortfall_count,
        valid_future_open_cover_count=summary.valid_future_open_cover_count,
        unknown_hold_count=summary.unknown_hold_count,
        ready_to_plan_count=summary.ready_to_plan_count,
        blocked_shortfall_count=summary.blocked_shortfall_count,
        required_new=summary.ready_to_plan_count,
        hard_hourly_required_new=max(0, int(hard_hourly_required_new)),
        reason=reason,
        next_decision_at=summary.next_decision_at,
        decision_snapshot=summary.as_dict(),
    ))
    task.stats = {**(task.stats or {}), "daily_fulfillment": summary.as_dict()}
    return summary


def daily_fulfillment_detail(
    session: Session,
    tenant_id: int,
    task_id: str,
    *,
    coverage_date: date,
    now: datetime | None = None,
) -> dict[str, object]:
    task = session.get(Task, task_id)
    if task is None or task.tenant_id != tenant_id or task.deleted_at is not None:
        raise ValueError("task not found")
    timestamp = now or _now()
    summary = summarize_daily_fulfillment(session, task, now=timestamp, coverage_date=coverage_date)
    rows = _current_rows(session, task, coverage_date)
    return {
        "coverage_date": coverage_date.isoformat(),
        **summary.as_dict(),
        "maximum_confirmable_count": _maximum_confirmable_count(rows, summary.frozen_denominator_count),
        "quality_funnel": _quality_funnel(rows),
        "generation_contract_funnel": _generation_contract_funnel(session, task, rows),
    }


def _current_rows(session: Session, task: Task, coverage_date: date) -> list[TaskAccountDailyCoverage]:
    return list(session.scalars(select(TaskAccountDailyCoverage).where(
        TaskAccountDailyCoverage.task_id == task.id,
        TaskAccountDailyCoverage.coverage_date == coverage_date,
    )))


def _maximum_confirmable_count(rows: list[TaskAccountDailyCoverage], frozen: int) -> int:
    terminal_codes = {"cannot_send", "membership_permission_denied", "target_ref_invalid"}
    blocked = sum(
        max(0, int(row.target_count or 1) - int(row.confirmed_count or 0))
        for row in rows if row.blocker_code in terminal_codes
    )
    return max(0, frozen - blocked)


def _quality_funnel(rows: list[TaskAccountDailyCoverage]) -> dict[str, int]:
    codes = {"1h_similar", "7d_semantic", "semantic_cluster", "check_in_repeat", "duplicate_message"}
    return _blocker_counts(rows, codes)


def _generation_contract_funnel(
    session: Session,
    task: Task,
    rows: list[TaskAccountDailyCoverage],
) -> dict[str, int]:
    blocked = sum(
        max(0, int(row.target_count or 1) - int(row.confirmed_count or 0))
        for row in rows if row.blocker_stage == "generation_contract"
    )
    audits = session.scalar(
        select(func.count(AiGenerationContractAudit.id)).where(AiGenerationContractAudit.task_id == task.id)
    ) or 0
    return {"blocked_coverage_count": blocked, "audit_count": int(audits)}


def _blocker_counts(rows: list[TaskAccountDailyCoverage], codes: set[str]) -> dict[str, int]:
    result: dict[str, int] = {}
    for row in rows:
        if row.blocker_code in codes:
            result[row.blocker_code] = result.get(row.blocker_code, 0) + max(
                0, int(row.target_count or 1) - int(row.confirmed_count or 0),
            )
    return result


def _reconcile_open_coverage_rows(
    session: Session,
    task: Task,
    rows: list[TaskAccountDailyCoverage],
    timestamp: datetime,
) -> None:
    actions = _open_actions_by_coverage(session, task, rows)
    if not actions:
        return
    for row in rows:
        action = actions.get(row.id)
        if row.state == "ready" and _is_valid_future_open(action, timestamp):
            _reserve_ready_row(row, action, timestamp)
        elif _is_overdue_open(action, timestamp):
            _mark_overdue_open_row(row, action, timestamp)
    session.flush()


def _reserve_ready_row(row: TaskAccountDailyCoverage, action: Action, timestamp: datetime) -> None:
    row.state = "reserved"
    row.reserved_action_id = action.id
    row.last_action_id = action.id
    row.next_decision_at = action.scheduled_at
    row.updated_at = timestamp


def _mark_overdue_open_row(row: TaskAccountDailyCoverage, action: Action, timestamp: datetime) -> None:
    if row.state not in {"ready", "reserved", "sending"}:
        return
    row.state = "unknown"
    row.reserved_action_id = action.id
    row.last_action_id = action.id
    row.blocker_code = "coverage_action_overdue"
    row.blocker_stage = "remote_reconcile"
    row.blocker_detail = "覆盖 Action 已到期仍未进入终态，等待执行事实核验"
    row.recovery_path = "remote_reconcile"
    row.next_decision_at = None
    row.updated_at = timestamp


def _open_actions_by_coverage(
    session: Session,
    task: Task,
    rows: list[TaskAccountDailyCoverage],
) -> dict[str, Action]:
    coverage_ids = [row.id for row in rows]
    action_ids = [str(row.reserved_action_id) for row in rows if row.reserved_action_id]
    filters = [Action.task_id == task.id, Action.status.in_(OPEN_ACTION_STATUSES)]
    relations = []
    if coverage_ids:
        relations.append(Action.payload["coverage_ledger_id"].as_string().in_(coverage_ids))
    if action_ids:
        relations.append(Action.id.in_(action_ids))
    if not relations:
        return {}
    actions = session.scalars(
        select(Action).where(*filters, or_(*relations)).order_by(Action.scheduled_at.asc(), Action.created_at.asc(), Action.id.asc())
    )
    by_coverage: dict[str, Action] = {}
    reserved_lookup = {str(row.reserved_action_id): row.id for row in rows if row.reserved_action_id}
    for action in actions:
        coverage_id = _coverage_id_for_action(action)
        if coverage_id:
            by_coverage.setdefault(coverage_id, action)
        if action.id in reserved_lookup:
            by_coverage.setdefault(reserved_lookup[action.id], action)
    return by_coverage


def _coverage_id_for_action(action: Action) -> str:
    payload = action.payload if isinstance(action.payload, dict) else {}
    return str(payload.get("coverage_ledger_id") or "")


def _actions_by_id(session: Session, rows: list[TaskAccountDailyCoverage]) -> dict[str, Action]:
    action_ids = [str(row.reserved_action_id) for row in rows if row.reserved_action_id]
    if not action_ids:
        return {}
    return {action.id: action for action in session.scalars(select(Action).where(Action.id.in_(action_ids)))}


def _state_counts(
    rows: list[TaskAccountDailyCoverage],
    actions: dict[str, Action],
    timestamp: datetime,
) -> dict[str, int | dict[str, int]]:
    counts: dict[str, int | dict[str, int]] = {
        "frozen": 0, "confirmed": 0, "ready": 0, "reserved": 0,
        "unknown": 0, "blocked": 0, "valid_open": 0, "overdue_open": 0, "sendable": 0, "blocked_shortfall": 0,
        "blockers": {},
    }
    for row in rows:
        remaining = max(0, int(row.target_count or 1) - int(row.confirmed_count or 0))
        counts["frozen"] += int(row.target_count or 1)
        counts["confirmed"] += min(int(row.target_count or 1), int(row.confirmed_count or 0))
        _count_row_state(counts, row, remaining, actions.get(row.reserved_action_id or ""), timestamp)
    return counts


def _count_row_state(
    counts: dict[str, int | dict[str, int]],
    row: TaskAccountDailyCoverage,
    remaining: int,
    action: Action | None,
    timestamp: datetime,
) -> None:
    if remaining <= 0:
        return
    if row.state == "ready":
        counts["ready"] += remaining
        counts["sendable"] += remaining
    elif row.state in {"reserved", "sending"}:
        counts["reserved"] += remaining
        if _is_valid_future_open(action, timestamp):
            counts["valid_open"] += remaining
            counts["sendable"] += remaining
    elif row.state == "unknown":
        counts["unknown"] += remaining
        if row.blocker_code == "coverage_action_overdue":
            counts["overdue_open"] += remaining
    elif row.state in {"blocked", "pending_admission", "admission_running"}:
        counts["blocked"] += remaining
        counts["blocked_shortfall"] += remaining
    code = str(row.blocker_code or "")
    if code:
        blockers = counts["blockers"]
        assert isinstance(blockers, dict)
        blockers[code] = int(blockers.get(code, 0)) + remaining


def _summary_from_counts(
    rows: list[TaskAccountDailyCoverage],
    counts: dict[str, int | dict[str, int]],
    timestamp: datetime,
) -> DailyFulfillmentSummary:
    frozen = int(counts["frozen"])
    confirmed = int(counts["confirmed"])
    full_shortfall = max(0, frozen - confirmed)
    ready = int(counts["ready"])
    blocked = int(counts["blocked"])
    unknown = int(counts["unknown"])
    next_decision = _next_decision_at(rows, timestamp, ready)
    outcome = _daily_outcome(full_shortfall, blocked, unknown, ready)
    return DailyFulfillmentSummary(
        frozen, confirmed, ready, int(counts["reserved"]), unknown, blocked,
        full_shortfall, int(counts["valid_open"]), int(counts["overdue_open"]), ready, int(counts["blocked_shortfall"]), int(counts["sendable"]),
        dict(counts["blockers"]), outcome, next_decision,
    )


def _is_valid_future_open(action: Action | None, timestamp: datetime) -> bool:
    return bool(action and action.status in OPEN_ACTION_STATUSES and is_after_or_equal(action.scheduled_at, timestamp))


def _is_overdue_open(action: Action | None, timestamp: datetime) -> bool:
    return bool(action and action.status in OPEN_ACTION_STATUSES and is_before(action.scheduled_at, timestamp))


def _next_decision_at(rows: list[TaskAccountDailyCoverage], timestamp: datetime, ready_count: int) -> datetime | None:
    ready_values = [
        row.next_decision_at
        for row in rows
        if row.state == "ready" and row.next_decision_at is not None
    ]
    if ready_values:
        return min(ready_values)
    if ready_count > 0:
        return timestamp + timedelta(seconds=DECISION_RECHECK_SECONDS)
    values = [row.next_decision_at or row.next_eligible_at for row in rows if row.next_decision_at or row.next_eligible_at]
    return min(values) if values else None


def _daily_outcome(full_shortfall: int, blocked: int, unknown: int, ready: int) -> str:
    if full_shortfall == 0 and unknown == 0:
        return "met"
    if blocked > 0:
        return "blocked"
    if ready > 0 or unknown > 0:
        return "at_risk"
    return "feasible"


__all__ = [
    "DailyFulfillmentSummary",
    "daily_fulfillment_detail",
    "record_daily_fulfillment_decision",
    "summarize_daily_fulfillment",
]

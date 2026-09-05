"""Project shared account usage without moving an original reservation or call."""
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from sqlalchemy import and_, or_, select

from app.models import (
    AccountBehaviorBudgetLedger, AccountBehaviorBudgetReservation, Action, ExecutionAttempt,
)
from app.timezone import BEIJING_TZ, as_beijing

from .engagement_action_classes import ACTION_CLASS_BY_TYPE
from .engagement_legacy_occupancy import LegacyOccupancyScope, read_legacy_attempt_occupancy
from .engagement_runtime_error import RuntimeResourceBlocked


OCCUPIED_BUDGET_STATES = ("reserved", "call_issued", "unknown", "confirmed", "unowned")
RESERVATION_OCCUPIED_STATES = ("reserved", "call_issued", "unknown", "confirmed")


@dataclass(frozen=True)
class SharedUsageScope:
    tenant_id: int
    account_id: int
    original_day: date
    activity_day: date


@dataclass(frozen=True)
class SharedAccountUsage:
    original_extra: tuple[tuple[str, int], ...]
    activity_occupied: tuple[tuple[str, int], ...]
    legacy_inflight: tuple[str, ...]
    issues: tuple[str, ...]


def read_shared_account_usage(session, scope: SharedUsageScope, *, excluded_attempt_id=""):
    with session.no_autoflush:
        legacy = read_legacy_attempt_occupancy(session, LegacyOccupancyScope(
            tenant_id=scope.tenant_id, account_ids=(scope.account_id,),
            task_day=scope.original_day, additional_task_days=(scope.activity_day,)))
        rows = tuple(session.execute(_reservation_query(scope, excluded_attempt_id)).mappings())
    original = Counter(row.action_class for row in legacy if row.original_task_day == scope.original_day)
    activity = Counter(row.action_class for row in legacy if row.call_day == scope.activity_day)
    issues = [code for row in legacy for code in row.issues]
    for row in rows:
        issues.extend(_reservation_issues(row))
        if row["state"] == "reserved" or _activity_day(row) == scope.activity_day:
            activity[row["action_class"]] += row["amount"]
    return SharedAccountUsage(tuple(sorted(original.items())), tuple(sorted(activity.items())),
        tuple(row.attempt_id for row in legacy if row.remote_inflight), tuple(sorted(set(issues))))


def _reservation_query(scope, excluded_attempt_id):
    reservation, ledger, attempt = AccountBehaviorBudgetReservation, AccountBehaviorBudgetLedger, ExecutionAttempt
    start = datetime.combine(scope.activity_day, time.min, tzinfo=BEIJING_TZ)
    return (select(reservation.id, reservation.state, reservation.amount, reservation.action_class,
        reservation.task_id, reservation.action_id, reservation.attempt_id,
        ledger.tenant_id, ledger.account_id, ledger.task_day,
        attempt.tenant_id.label("attempt_tenant"), attempt.account_id.label("attempt_account"),
        attempt.action_id.label("attempt_action"), attempt.task_lifecycle_epoch.label("attempt_epoch"),
        attempt.gateway_call_started_at.label("call_at"),
        Action.tenant_id.label("action_tenant"), Action.account_id.label("action_account"),
        Action.task_id.label("action_task"), Action.task_lifecycle_epoch.label("action_epoch"),
        Action.action_type).select_from(reservation)
        .join(ledger, ledger.id == reservation.ledger_id)
        .join(attempt, attempt.id == reservation.attempt_id)
        .join(Action, Action.id == reservation.action_id)
        .where(ledger.tenant_id == scope.tenant_id, ledger.account_id == scope.account_id,
            reservation.state.in_(RESERVATION_OCCUPIED_STATES),
            reservation.attempt_id != excluded_attempt_id,
            or_(ledger.task_day == scope.original_day, reservation.state == "reserved",
                attempt.gateway_call_started_at.is_(None),
                and_(attempt.gateway_call_started_at >= start,
                    attempt.gateway_call_started_at < start + timedelta(days=1)))))


def _reservation_issues(row):
    issues = []
    if (row["tenant_id"], row["account_id"], row["action_id"]) != (
            row["attempt_tenant"], row["attempt_account"], row["attempt_action"]):
        issues.append("budget_reservation_attempt_owner_mismatch")
    if (row["tenant_id"], row["account_id"], row["task_id"], row["attempt_epoch"]) != (
            row["action_tenant"], row["action_account"], row["action_task"], row["action_epoch"]):
        issues.append("budget_reservation_action_owner_mismatch")
    if row["action_class"] != ACTION_CLASS_BY_TYPE.get(row["action_type"]):
        issues.append("budget_reservation_action_class_mismatch")
    if row["amount"] <= 0:
        issues.append("budget_reservation_amount_invalid")
    if row["state"] != "reserved" and row["call_at"] is None:
        issues.append("actual_call_day_unproven")
    return tuple(issues)


def _activity_day(row):
    return as_beijing(row["call_at"]).date() if row["call_at"] is not None else None


def assert_shared_evidence(usage):
    if usage.issues:
        raise RuntimeResourceBlocked("account_shared_usage_unproven",
            "账号原调用占用证据未闭合:" + ",".join(usage.issues))
    if usage.legacy_inflight:
        raise RuntimeResourceBlocked("account_legacy_remote_inflight", "账号旧调用尚未证明传输结束")


def original_budget_occupancy(ledger, usage):
    occupied = Counter(behavior_occupancy(ledger))
    occupied.update(dict(usage.original_extra))
    return dict(occupied)


def behavior_occupancy(ledger):
    return {action_class: sum(int(states.get(key) or 0) for key in OCCUPIED_BUDGET_STATES)
        for action_class, states in (ledger.counters or {}).items() if isinstance(states, dict)}


def activity_budget_source(session, scope, policy):
    # The caller holds the account lock, also used by the unowned-activity writer.
    return session.scalar(select(AccountBehaviorBudgetLedger).where(
        AccountBehaviorBudgetLedger.tenant_id == scope.tenant_id,
        AccountBehaviorBudgetLedger.account_id == scope.account_id,
        AccountBehaviorBudgetLedger.task_day == scope.activity_day,
    ).with_for_update().execution_options(populate_existing=True)) or policy


def activity_budget_occupancy(source, usage):
    occupied = Counter(dict(usage.activity_occupied))
    if isinstance(source, AccountBehaviorBudgetLedger):
        occupied.update({action_class: int(states.get("unowned") or 0)
            for action_class, states in (source.counters or {}).items() if isinstance(states, dict)})
    return dict(occupied)

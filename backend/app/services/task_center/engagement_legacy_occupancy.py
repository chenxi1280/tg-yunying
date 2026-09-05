"""Read original, unreserved calls without rewriting their fulfillment identity."""
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from itertools import groupby

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.models import (AccountBehaviorBudgetReservation, Action, ExecutionAttempt,
    GatewayRequestEvidenceJournal, TaskDayLedger)
from app.timezone import BEIJING_TZ, as_beijing

from .engagement_action_classes import ACTION_CLASS_BY_TYPE
from .engagement_binding import ENGAGEMENT_TASK_TYPES
from .engagement_gateway_return import journal_matches_original_call, journal_proves_gateway_return

TERMINAL_ATTEMPT_STATES = frozenset({
    "success", "failed", "result_unknown", "skipped_before_gateway", "call_not_started",
    "skipped", "cancelled", "permanent_failed",
})


@dataclass(frozen=True)
class LegacyOccupancyScope:
    tenant_id: int
    account_ids: tuple[int, ...]
    task_day: date
    additional_task_days: tuple[date, ...] = ()

    def __post_init__(self):
        if self.tenant_id <= 0 or not self.account_ids:
            raise ValueError("legacy_occupancy_scope_required")
        if any(item <= 0 for item in self.account_ids):
            raise ValueError("legacy_occupancy_account_invalid")
        if len(set(self.account_ids)) != len(self.account_ids):
            raise ValueError("legacy_occupancy_account_duplicate")


@dataclass(frozen=True)
class LegacyAttemptOccupancy:
    attempt_id: str
    action_id: str
    task_id: str
    account_id: int
    action_class: str
    original_task_day: date | None
    call_day: date | None
    attempt_state: str
    remote_inflight: bool
    issues: tuple[str, ...]


def read_legacy_attempt_occupancy(
    session: Session, scope: LegacyOccupancyScope,
) -> tuple[LegacyAttemptOccupancy, ...]:
    with session.no_autoflush:
        rows = tuple(session.execute(_candidate_query(scope,
            dialect_name=session.get_bind().dialect.name)).mappings())
    projected = (_project_rows(tuple(group)) for _, group in groupby(rows, key=lambda r: r["attempt_id"]))
    return tuple(item for item in projected if item is not None)


def _project_rows(rows):
    return _project_attempt(rows[0], tuple(row for row in rows if row["journal_id"] is not None))


def _candidate_query(scope, *, dialect_name):
    snapshot = ExecutionAttempt.result_snapshot
    ledger_ref = Action.payload["task_day_ledger_id"].as_string()
    columns = _candidate_columns(ledger_ref, snapshot, dialect_name=dialect_name)
    return (select(*columns)
        .join(Action, Action.id == ExecutionAttempt.action_id)
        .outerjoin(TaskDayLedger, TaskDayLedger.id == ledger_ref)
        .outerjoin(GatewayRequestEvidenceJournal,
            GatewayRequestEvidenceJournal.execution_attempt_id == ExecutionAttempt.id)
        .where(ExecutionAttempt.tenant_id == scope.tenant_id,
            ExecutionAttempt.account_id.in_(scope.account_ids),
            Action.task_type.in_(ENGAGEMENT_TASK_TYPES),
            Action.action_type.in_(ACTION_CLASS_BY_TYPE),
            ~select(AccountBehaviorBudgetReservation.id).where(
                AccountBehaviorBudgetReservation.attempt_id == ExecutionAttempt.id).exists(),
            or_(ExecutionAttempt.gateway_call_started_at.is_not(None),
                ExecutionAttempt.status.in_(("success", "result_unknown"))),
            or_(_requested_days(scope),
                and_(ExecutionAttempt.status != "success",
                    or_(snapshot["transport_termination_state"].as_string().is_(None),
                        snapshot["transport_termination_state"].as_string() != "acknowledged"))))
        .order_by(ExecutionAttempt.id))


def _requested_days(scope):
    dates = frozenset((scope.task_day, *scope.additional_task_days))
    periods = tuple((datetime.combine(day, time.min, tzinfo=BEIJING_TZ),
        datetime.combine(day + timedelta(days=1), time.min, tzinfo=BEIJING_TZ)) for day in dates)
    return or_(TaskDayLedger.obligation_local_date.in_(dates),
        *(and_(Action.pacing_due_at >= start, Action.pacing_due_at < end) for start, end in periods),
        *(and_(ExecutionAttempt.gateway_call_started_at >= start,
            ExecutionAttempt.gateway_call_started_at < end) for start, end in periods))


def _candidate_columns(ledger_ref, snapshot, *, dialect_name):
    return (
        ExecutionAttempt.id.label("attempt_id"), ExecutionAttempt.action_id,
        ExecutionAttempt.tenant_id, ExecutionAttempt.account_id,
        ExecutionAttempt.task_lifecycle_epoch.label("attempt_epoch"),
        ExecutionAttempt.status.label("attempt_state"),
        ExecutionAttempt.gateway_call_started_at.label("call_at"),
        ExecutionAttempt.after_call_at,
        ExecutionAttempt.remote_message_id.label("remote_message_id"),
        snapshot["remote_mutation_started"].label("mutation_started"),
        _mutation_type_column(snapshot, dialect_name).label("mutation_json_type"),
        snapshot["transport_termination_state"].as_string().label("termination"),
        snapshot["gateway_request_identity"].as_string().label("attempt_request"),
        snapshot["gateway_request_fingerprint"].as_string().label("attempt_request_hash"),
        snapshot["gateway_target_fingerprint"].as_string().label("attempt_target_hash"),
        Action.tenant_id.label("action_tenant"), Action.task_id, Action.action_type,
        Action.account_id.label("action_account"),
        Action.task_lifecycle_epoch.label("action_epoch"), Action.pacing_due_at,
        ledger_ref.label("ledger_ref"), TaskDayLedger.id.label("ledger_id"),
        TaskDayLedger.tenant_id.label("ledger_tenant"),
        TaskDayLedger.task_id.label("ledger_task"),
        TaskDayLedger.obligation_local_date.label("ledger_day"),
        GatewayRequestEvidenceJournal.id.label("journal_id"),
        GatewayRequestEvidenceJournal.tenant_id.label("journal_tenant"),
        GatewayRequestEvidenceJournal.action_id.label("journal_action"),
        GatewayRequestEvidenceJournal.account_id.label("journal_account"),
        GatewayRequestEvidenceJournal.remote_mutation_state.label("journal_mutation"),
        GatewayRequestEvidenceJournal.state.label("journal_state"),
        *_journal_return_columns(),
    )


def _mutation_type_column(snapshot, dialect_name):
    if dialect_name == "sqlite":
        return func.json_type(snapshot, "$.remote_mutation_started")
    return func.json_typeof(snapshot["remote_mutation_started"])


def _journal_return_columns():
    journal = GatewayRequestEvidenceJournal
    return (journal.gateway_request_identity.label("journal_request"),
        journal.request_fingerprint.label("journal_request_hash"),
        journal.target_fingerprint.label("journal_target_hash"),
        journal.result_fingerprint.label("journal_result_hash"),
        journal.evidence_hash.label("journal_evidence_hash"),
        journal.remote_message_id.label("journal_message_id"),
        journal.remote_fact_id.label("journal_fact_id"),
        journal.typed_remote_fact.label("journal_typed_fact"),
        journal.failure_code.label("journal_failure_code"),
        journal.observed_at.label("journal_observed_at"))


def _project_attempt(row, journals):
    mutation, mutation_issues = _mutation_state(row, journals)
    issues = list(_owner_issues(row) + mutation_issues)
    if mutation == "false" and not issues:
        return None
    original_day = _original_day(row)
    call_day = as_beijing(row["call_at"]).date() if row["call_at"] else None
    if original_day is None:
        issues.append("original_task_day_unproven")
    if call_day is None:
        issues.append("actual_call_day_unproven")
    terminated = row["termination"] == "acknowledged"
    terminated = terminated or (row["attempt_state"] == "success" and mutation == "true")
    if not (_owner_issues(row) or mutation_issues):
        terminated = terminated or any(journal_proves_gateway_return(row, item) for item in journals)
    return LegacyAttemptOccupancy(
        attempt_id=row["attempt_id"], action_id=row["action_id"], task_id=row["task_id"],
        account_id=row["account_id"], action_class=ACTION_CLASS_BY_TYPE[row["action_type"]],
        original_task_day=original_day, call_day=call_day,
        attempt_state=row["attempt_state"], remote_inflight=not terminated,
        issues=tuple(issues),
    )


def _owner_issues(row):
    issues = []
    if row["tenant_id"] != row["action_tenant"]:
        issues.append("attempt_action_tenant_mismatch")
    if row["account_id"] != row["action_account"]:
        issues.append("attempt_action_account_mismatch")
    if row["attempt_epoch"] != row["action_epoch"]:
        issues.append("attempt_action_epoch_mismatch")
    if row["ledger_ref"] and (row["ledger_id"] is None
            or row["ledger_tenant"] != row["tenant_id"] or row["ledger_task"] != row["task_id"]):
        issues.append("original_task_day_ledger_mismatch")
    return tuple(issues)


def _mutation_state(row, journals):
    states, issues = _journal_mutation_states(row, journals)
    # The journal is the durable Gateway outcome; a pre-call snapshot can be older.
    if not journals:
        observed, snapshot_issues = _snapshot_mutation(row)
        issues.extend(snapshot_issues)
        if _snapshot_is_terminal_evidence(row, observed):
            states.add("true" if observed else "false")
    if row["remote_message_id"] or row["attempt_state"] == "success":
        states.add("true")
    if len(states) > 1:
        issues.append("remote_mutation_evidence_conflict")
        return "unknown", tuple(issues)
    return next(iter(states), "unknown"), tuple(issues)


def _journal_mutation_states(row, journals):
    states = set()
    issues = []
    for journal in journals:
        if (journal["journal_tenant"], journal["journal_action"], journal["journal_account"]) != (
                row["tenant_id"], row["action_id"], row["account_id"]):
            issues.append("gateway_journal_owner_mismatch")
        if journal["journal_state"] == "conflict":
            issues.append("gateway_journal_evidence_conflict")
        if journal["journal_mutation"] in {"true", "false"}:
            states.add(journal["journal_mutation"])
            if not _journal_result_is_proven(row, journal):
                issues.append("gateway_journal_result_unproven")
    return states, issues


def _journal_result_is_proven(row, journal):
    if journal["journal_mutation"] == "false" and (
            journal["journal_message_id"] or journal["journal_fact_id"]):
        return False
    return journal_matches_original_call(row, journal)


def _snapshot_mutation(row):
    kind, value = row["mutation_json_type"], row["mutation_started"]
    if kind in {None, "null"}:
        return None, ()
    if kind in {"true", "false"}:
        return kind == "true", ()
    if kind == "boolean" and type(value) is bool:
        return value, ()
    return None, ("remote_mutation_snapshot_invalid",)


def _snapshot_is_terminal_evidence(row, observed):
    if observed is True:
        return True
    return (observed is False and row["after_call_at"] is not None
        and row["call_at"] is not None
        and as_beijing(row["after_call_at"]) >= as_beijing(row["call_at"])
        and row["attempt_state"] in TERMINAL_ATTEMPT_STATES)


def _original_day(row):
    if row["ledger_ref"]:
        return row["ledger_day"] if not any(
            code == "original_task_day_ledger_mismatch" for code in _owner_issues(row)) else None
    return as_beijing(row["pacing_due_at"]).date() if row["pacing_due_at"] else None

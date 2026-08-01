from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import and_, case, func, select

from app.database import SessionLocal
from app.models import (
    Action,
    ChannelMessage,
    ExecutionAttempt,
    SearchClickAssignmentEpoch,
    SearchClickFulfillmentObligation,
    SearchClickOpportunityAssignment,
    Task,
    TaskAccountDailyCoverage,
    TaskDayLedger,
    TaskGroupDailyMessageSlot,
    TaskGroupDailyTarget,
    ViewFulfillmentObligation,
    ViewRemoteFact,
)
from app.services.task_center.production_e4_diagnostics import (
    ai_open_action_details,
    search_claimed_details,
    view_open_details,
)


BEIJING = ZoneInfo("Asia/Shanghai")
TASK_IDS_ENV = "TASK_FULFILLMENT_E4_TASK_IDS"
RELEASE_LIVE_AT_ENV = "TASK_FULFILLMENT_RELEASE_LIVE_AT"
SUPPORTED_TASK_TYPES = {"group_ai_chat", "search_click", "channel_view"}
SAMPLE_LIMIT = 8


def parse_task_ids() -> list[str]:
    values = [value.strip() for value in os.getenv(TASK_IDS_ENV, "").split(",")]
    task_ids = list(dict.fromkeys(value for value in values if value))
    if not task_ids:
        raise ValueError(f"{TASK_IDS_ENV} is required")
    return task_ids


def parse_release_since() -> datetime:
    raw = os.getenv(RELEASE_LIVE_AT_ENV, "").strip()
    if not raw:
        raise ValueError(f"{RELEASE_LIVE_AT_ENV} is required")
    value = datetime.fromisoformat(raw)
    return value.replace(tzinfo=BEIJING) if value.tzinfo is None else value.astimezone(BEIJING)


def iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def json_line(label: str, payload: dict[str, Any]) -> None:
    print(f"{label}=" + json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)


def _latest_ledger(session, task_id: str) -> TaskDayLedger | None:
    return session.scalar(
        select(TaskDayLedger)
        .where(TaskDayLedger.task_id == task_id)
        .order_by(TaskDayLedger.period_start_at.desc())
        .limit(1)
    )


def _planner_error_after(task: Task, since: datetime) -> dict[str, Any] | None:
    error = dict((task.stats or {}).get("planner_runtime_error") or {})
    if not error:
        return None
    raw = str(error.get("recorded_at") or "").strip()
    if not raw:
        return error
    recorded_at = datetime.fromisoformat(raw)
    recorded_at = recorded_at.replace(tzinfo=BEIJING) if recorded_at.tzinfo is None else recorded_at
    return error if recorded_at >= since else None


def _action_counts(session, task_id: str, since: datetime) -> dict[str, int]:
    rows = session.execute(
        select(Action.status, func.count(Action.id))
        .where(Action.task_id == task_id, Action.created_at >= since)
        .group_by(Action.status)
    )
    return {str(status): int(count) for status, count in rows}


def _attempt_snapshot(session, task_id: str, since: datetime) -> dict[str, Any]:
    observed_at = func.coalesce(ExecutionAttempt.after_call_at, ExecutionAttempt.created_at)
    base = (
        select(ExecutionAttempt)
        .join(Action, Action.id == ExecutionAttempt.action_id)
        .where(Action.task_id == task_id, observed_at >= since)
    )
    status_rows = session.execute(
        select(ExecutionAttempt.status, func.count(ExecutionAttempt.id))
        .join(Action, Action.id == ExecutionAttempt.action_id)
        .where(Action.task_id == task_id, observed_at >= since)
        .group_by(ExecutionAttempt.status)
    )
    remote_success = session.scalar(
        select(func.count(ExecutionAttempt.id))
        .join(Action, Action.id == ExecutionAttempt.action_id)
        .where(
            Action.task_id == task_id,
            observed_at >= since,
            ExecutionAttempt.status == "success",
            ExecutionAttempt.remote_message_id != "",
        )
    )
    samples = list(
        session.scalars(
            base
            .order_by(ExecutionAttempt.created_at.desc())
            .limit(SAMPLE_LIMIT)
        )
    )
    status_counts = {str(status): int(count) for status, count in status_rows}
    return {
        "post_release_count": sum(status_counts.values()),
        "post_release_status_counts": status_counts,
        "post_release_remote_success_count": int(remote_success or 0),
        "samples": [_attempt_row(row) for row in samples],
    }


def _attempt_row(attempt: ExecutionAttempt) -> dict[str, Any]:
    return {
        "id": attempt.id,
        "action_id": attempt.action_id,
        "status": attempt.status,
        "account_id": attempt.account_id,
        "remote_message_id": attempt.remote_message_id,
        "failure_type": attempt.failure_type,
        "failure_detail": str(attempt.failure_detail or "")[:240],
        "gateway_call_started_at": iso(attempt.gateway_call_started_at),
        "after_call_at": iso(attempt.after_call_at),
    }


def _group_daily_snapshot(session, ledger: TaskDayLedger) -> dict[str, int]:
    target = session.execute(
        select(
            func.count(TaskGroupDailyTarget.id),
            func.coalesce(func.sum(TaskGroupDailyTarget.due_message_count), 0),
            func.coalesce(func.sum(TaskGroupDailyTarget.confirmed_message_count), 0),
        ).where(TaskGroupDailyTarget.task_day_ledger_id == ledger.id)
    ).one()
    coverage = session.execute(
        select(
            func.count(TaskAccountDailyCoverage.id),
            func.coalesce(func.sum(_coverage_confirmed_case()), 0),
        ).where(TaskAccountDailyCoverage.task_day_ledger_id == ledger.id)
    ).one()
    return {
        "target_row_count": int(target[0]),
        "due_message_count": int(target[1]),
        "confirmed_message_count": int(target[2]),
        "coverage_required_count": int(coverage[0]),
        "coverage_confirmed_count": int(coverage[1]),
    }


def _group_runtime_snapshot(session, ledger: TaskDayLedger) -> dict[str, Any]:
    coverage_rows = session.execute(
        select(
            TaskAccountDailyCoverage.state,
            TaskAccountDailyCoverage.blocker_code,
            func.count(TaskAccountDailyCoverage.id),
        )
        .where(TaskAccountDailyCoverage.task_day_ledger_id == ledger.id)
        .group_by(TaskAccountDailyCoverage.state, TaskAccountDailyCoverage.blocker_code)
    )
    slot_rows = session.execute(
        select(
            TaskGroupDailyMessageSlot.slot_kind,
            TaskGroupDailyMessageSlot.state,
            func.count(TaskGroupDailyMessageSlot.id),
        )
        .where(TaskGroupDailyMessageSlot.task_day_ledger_id == ledger.id)
        .group_by(TaskGroupDailyMessageSlot.slot_kind, TaskGroupDailyMessageSlot.state)
    )
    actions = list(session.scalars(select(Action).where(
        Action.task_id == ledger.task_id,
        Action.action_type == "send_message",
        Action.payload["task_day_ledger_id"].as_string() == ledger.id,
        Action.status.in_(("pending", "claiming", "executing")),
    )))
    action_counts: dict[str, int] = {}
    for action in actions:
        payload = action.payload or {}
        key = f"{action.status}:{payload.get('ai_generation_status') or ''}:{'ready' if str(payload.get('message_text') or '').strip() else 'empty'}"
        action_counts[key] = action_counts.get(key, 0) + 1
    return {
        "open_action_counts": dict(sorted(action_counts.items())),
        **ai_open_action_details(session, ledger, actions),
        "coverage_counts": [
            {"state": state, "blocker_code": blocker or "", "count": int(count)}
            for state, blocker, count in coverage_rows
        ],
        "quantity_slot_counts": [
            {"slot_kind": kind, "state": state, "count": int(count)}
            for kind, state, count in slot_rows
        ],
    }


def _coverage_confirmed_case():
    return case(
        (
            and_(
                TaskAccountDailyCoverage.confirmed_count >= TaskAccountDailyCoverage.target_count,
                TaskAccountDailyCoverage.last_remote_message_id != "",
            ),
            1,
        ),
        else_=0,
    )


def _search_snapshot(session, ledger: TaskDayLedger, since: datetime) -> dict[str, int]:
    row = session.execute(
        select(
            func.count(SearchClickFulfillmentObligation.id),
            func.coalesce(func.sum(_search_confirmed_case()), 0),
            func.coalesce(func.sum(_search_post_release_case(since)), 0),
        ).where(SearchClickFulfillmentObligation.task_day_ledger_id == ledger.id)
    ).one()
    return {
        "required_count": int(row[0]),
        "confirmed_count": int(row[1]),
        "post_release_confirmed_count": int(row[2]),
    }


def _search_runtime_snapshot(session, ledger: TaskDayLedger) -> dict[str, Any]:
    assignment_rows = session.execute(
        select(
            SearchClickOpportunityAssignment.state,
            SearchClickOpportunityAssignment.release_reason,
            func.count(SearchClickOpportunityAssignment.id),
        )
        .where(SearchClickOpportunityAssignment.task_day_ledger_id == ledger.id)
        .group_by(SearchClickOpportunityAssignment.state, SearchClickOpportunityAssignment.release_reason)
    )
    epoch_rows = session.execute(
        select(
            SearchClickAssignmentEpoch.finalize_status,
            SearchClickAssignmentEpoch.outcome,
            func.count(func.distinct(SearchClickAssignmentEpoch.id)),
        )
        .join(
            SearchClickOpportunityAssignment,
            SearchClickOpportunityAssignment.search_click_assignment_epoch_id
            == SearchClickAssignmentEpoch.id,
        )
        .where(SearchClickOpportunityAssignment.task_day_ledger_id == ledger.id)
        .group_by(SearchClickAssignmentEpoch.finalize_status, SearchClickAssignmentEpoch.outcome)
    )
    return {
        "assignment_counts": [
            {"state": state, "release_reason": reason or "", "count": int(count)}
            for state, reason, count in assignment_rows
        ],
        "epoch_counts": [
            {"finalize_status": status, "outcome": outcome, "count": int(count)}
            for status, outcome, count in epoch_rows
        ],
        **search_claimed_details(session, ledger),
    }


def _search_confirmed_case():
    return case(
        (
            and_(
                SearchClickFulfillmentObligation.status == "confirmed",
                SearchClickFulfillmentObligation.target_click_observed.is_(True),
                SearchClickFulfillmentObligation.click_evidence_hash.is_not(None),
            ),
            1,
        ),
        else_=0,
    )


def _search_post_release_case(since: datetime):
    return case(
        (
            and_(
                SearchClickFulfillmentObligation.status == "confirmed",
                SearchClickFulfillmentObligation.target_click_observed.is_(True),
                SearchClickFulfillmentObligation.click_evidence_hash.is_not(None),
                SearchClickFulfillmentObligation.remote_confirmed_at >= since,
            ),
            1,
        ),
        else_=0,
    )


def _view_snapshot(session, ledger: TaskDayLedger, since: datetime) -> dict[str, int]:
    required, confirmed = session.execute(
        select(
            func.count(ViewFulfillmentObligation.id),
            func.coalesce(func.sum(case((ViewFulfillmentObligation.status == "confirmed", 1), else_=0)), 0),
        ).where(ViewFulfillmentObligation.task_day_ledger_id == ledger.id)
    ).one()
    fact_query = select(
        func.count(ViewRemoteFact.id),
        func.coalesce(func.sum(case((ViewRemoteFact.remote_confirmed_at >= since, 1), else_=0)), 0),
    ).join(ViewFulfillmentObligation, ViewFulfillmentObligation.id == ViewRemoteFact.obligation_id)
    facts = session.execute(
        fact_query.where(ViewFulfillmentObligation.task_day_ledger_id == ledger.id)
    ).one()
    return {
        "required_count": int(required),
        "confirmed_count": int(confirmed),
        "remote_fact_count": int(facts[0]),
        "post_release_remote_fact_count": int(facts[1]),
    }


def _view_message_snapshot(session, ledger: TaskDayLedger) -> list[dict[str, Any]]:
    rows = session.execute(
        select(
            ViewFulfillmentObligation.channel_message_id,
            ViewFulfillmentObligation.status,
            func.count(ViewFulfillmentObligation.id),
        )
        .where(ViewFulfillmentObligation.task_day_ledger_id == ledger.id)
        .group_by(ViewFulfillmentObligation.channel_message_id, ViewFulfillmentObligation.status)
        .order_by(ViewFulfillmentObligation.channel_message_id)
    )
    messages: dict[int, dict[str, Any]] = {}
    for message_id, status, count in rows:
        item = messages.setdefault(int(message_id), {"channel_message_id": int(message_id), "status_counts": {}})
        item["status_counts"][str(status)] = int(count)
    for item in messages.values():
        message = session.get(ChannelMessage, item["channel_message_id"])
        item["remote_message_id"] = int(message.message_id) if message else None
        item["published_at"] = iso(message.published_at) if message else None
    return list(messages.values())


def task_snapshot(session, task_id: str, since: datetime) -> dict[str, Any]:
    task = session.get(Task, task_id)
    if task is None:
        return {"task_id": task_id, "task_type": "", "task_status": "missing", "ledger_id": None}
    ledger = _latest_ledger(session, task_id)
    snapshot = _base_task_snapshot(session, task, ledger, since)
    if ledger is None:
        return snapshot
    if task.type == "group_ai_chat":
        snapshot["group_daily"] = _group_daily_snapshot(session, ledger)
        snapshot["group_runtime"] = _group_runtime_snapshot(session, ledger)
    elif task.type == "search_click":
        snapshot["search_click"] = _search_snapshot(session, ledger, since)
        snapshot["search_runtime"] = _search_runtime_snapshot(session, ledger)
    elif task.type == "channel_view":
        snapshot["channel_view"] = _view_snapshot(session, ledger, since)
        snapshot["channel_view_messages"] = _view_message_snapshot(session, ledger)
        snapshot["view_runtime"] = view_open_details(session, ledger)
    return snapshot


def _base_task_snapshot(session, task: Task, ledger: TaskDayLedger | None, since: datetime) -> dict[str, Any]:
    return {
        "task_id": task.id,
        "name": task.name,
        "task_type": task.type,
        "task_status": task.status,
        "last_error": task.last_error,
        "next_run_at": iso(task.next_run_at),
        "ledger_id": ledger.id if ledger else None,
        "ledger_day_phase": ledger.day_phase if ledger else None,
        "ledger_local_date": str(ledger.obligation_local_date) if ledger else None,
        "ledger_deadline_at": iso(ledger.deadline_at) if ledger else None,
        "planner_runtime_error": _planner_error_after(task, since),
        "post_release_action_counts": _action_counts(session, task.id, since),
        "attempts": _attempt_snapshot(session, task.id, since),
    }


def e4_blockers(snapshot: dict[str, Any]) -> list[str]:
    blockers = _common_blockers(snapshot)
    if not snapshot.get("ledger_id"):
        return blockers
    task_type = str(snapshot.get("task_type") or "")
    if task_type == "group_ai_chat":
        blockers.extend(_group_blockers(snapshot))
    elif task_type == "search_click":
        blockers.extend(_search_blockers(snapshot))
    elif task_type == "channel_view":
        blockers.extend(_view_blockers(snapshot))
    elif task_type not in SUPPORTED_TASK_TYPES:
        blockers.append("unsupported_task_type")
    return blockers


def _common_blockers(snapshot: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    if snapshot.get("task_status") == "missing":
        blockers.append("task_missing")
    elif snapshot.get("task_status") not in {"running", "completed"}:
        blockers.append("task_not_active")
    if not snapshot.get("ledger_id"):
        blockers.append("task_day_ledger_missing")
    if snapshot.get("planner_runtime_error"):
        blockers.append("planner_runtime_error")
    return blockers


def _group_blockers(snapshot: dict[str, Any]) -> list[str]:
    daily = dict(snapshot.get("group_daily") or {})
    blockers: list[str] = []
    if int(daily.get("target_row_count") or 0) <= 0:
        blockers.append("ai_daily_target_missing")
    if int(daily.get("confirmed_message_count") or 0) < int(daily.get("due_message_count") or 0):
        blockers.append("ai_daily_due_unmet")
    if int(daily.get("coverage_confirmed_count") or 0) < int(daily.get("coverage_required_count") or 0):
        blockers.append("ai_daily_coverage_unmet")
    if int((snapshot.get("attempts") or {}).get("post_release_remote_success_count") or 0) <= 0:
        blockers.append("ai_post_release_remote_fact_missing")
    return blockers


def _search_blockers(snapshot: dict[str, Any]) -> list[str]:
    click = dict(snapshot.get("search_click") or {})
    required = int(click.get("required_count") or 0)
    blockers = ["search_click_obligation_missing"] if required <= 0 else []
    if int(click.get("confirmed_count") or 0) < required:
        blockers.append("search_click_unmet")
    if int(click.get("post_release_confirmed_count") or 0) <= 0:
        blockers.append("search_click_post_release_fact_missing")
    return blockers


def _view_blockers(snapshot: dict[str, Any]) -> list[str]:
    view = dict(snapshot.get("channel_view") or {})
    required = int(view.get("required_count") or 0)
    blockers = ["channel_view_obligation_missing"] if required <= 0 else []
    if int(view.get("confirmed_count") or 0) < required:
        blockers.append("channel_view_unmet")
    if int(view.get("remote_fact_count") or 0) < int(view.get("confirmed_count") or 0):
        blockers.append("channel_view_remote_fact_missing")
    if int(view.get("post_release_remote_fact_count") or 0) <= 0:
        blockers.append("channel_view_post_release_fact_missing")
    return blockers


def main() -> None:
    since = parse_release_since()
    task_ids = parse_task_ids()
    rows: list[dict[str, Any]] = []
    with SessionLocal() as session:
        for task_id in task_ids:
            snapshot = task_snapshot(session, task_id, since)
            snapshot["blockers"] = e4_blockers(snapshot)
            rows.append(snapshot)
            json_line("TASK_FULFILLMENT_E4_TASK", snapshot)
    blockers = [{"task_id": row["task_id"], "blockers": row["blockers"]} for row in rows if row["blockers"]]
    summary = {"release_live_at": iso(since), "task_count": len(rows), "blocker_count": len(blockers), "blockers": blockers}
    json_line("TASK_FULFILLMENT_E4_SUMMARY", summary)
    if blockers:
        raise SystemExit("task fulfillment E4 gate failed")


if __name__ == "__main__":
    main()

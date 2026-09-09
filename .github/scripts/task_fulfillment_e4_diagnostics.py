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
    ChannelViewDailyMessageTarget,
    SearchClickAssignmentEpoch,
    SearchClickFulfillmentObligation,
    SearchClickOpportunityAssignment,
    SearchClickSolverCarrierUnitBinding, SearchClickSolverProblemSnapshot,
    Task,
    TaskDayLedger,
    ViewFulfillmentObligation,
    ViewRemoteFact,
)
from app.services.task_center.production_e4_diagnostics import (
    search_claimed_details,
    view_open_details,
)
from app.services.task_center.channel_fulfillment import (
    view_materialized_account_ids_for_messages,
)
from app.services.task_center.channel_view_targets import (
    channel_view_target_due,
    target_messages,
)


from app.services.task_center.production_e4_attempts import (
    BUSINESS_ACTION_TYPES, _attempt_snapshot,
)
from app.services.task_center.production_e4_group import (
    _group_daily_snapshot, _group_runtime_snapshot,
)
from app.services.task_center.production_e4_blockers import e4_blockers
from app.services.task_center.production_e4_scope import (
    configure_readonly_snapshot, discover_channel_view_task_ids,
)


BEIJING = ZoneInfo("Asia/Shanghai")
TASK_IDS_ENV = "TASK_FULFILLMENT_E4_TASK_IDS"
RELEASE_LIVE_AT_ENV = "TASK_FULFILLMENT_RELEASE_LIVE_AT"
SAMPLE_LIMIT = 8
DISCOVER_CHANNEL_VIEW = "__discover_channel_view__"


def parse_task_ids(session) -> list[str]:
    values = [value.strip() for value in os.getenv(TASK_IDS_ENV, "").split(",")]
    task_ids = list(dict.fromkeys(value for value in values if value))
    if task_ids and task_ids != [DISCOVER_CHANNEL_VIEW]:
        return task_ids
    return discover_channel_view_task_ids(session)

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

def _action_counts(session, task: Task, since: datetime) -> dict[str, int]:
    action_type = BUSINESS_ACTION_TYPES.get(task.type)
    rows = session.execute(
        select(Action.status, func.count(Action.id))
        .where(
            Action.task_id == task.id,
            Action.created_at >= since,
            Action.action_type == action_type,
        )
        .group_by(Action.status)
    )
    return {str(status): int(count) for status, count in rows}


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
            SearchClickSolverProblemSnapshot,
            SearchClickSolverProblemSnapshot.search_click_assignment_epoch_id
            == SearchClickAssignmentEpoch.id,
        )
        .join(
            SearchClickSolverCarrierUnitBinding,
            SearchClickSolverCarrierUnitBinding.search_click_solver_snapshot_id
            == SearchClickSolverProblemSnapshot.id,
        )
        .where(SearchClickSolverCarrierUnitBinding.task_id == ledger.task_id)
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


def _view_snapshot(
    session,
    task: Task,
    ledger: TaskDayLedger,
    since: datetime,
) -> dict[str, Any]:
    task_stats = dict(task.stats or {})
    due = _view_due_snapshot(session, task, ledger, since)
    return {
        "required_count": due["expected_due_count"],
        "expected_due_count": due["expected_due_count"],
        "materialized_count": due["materialized_count"],
        "materialization_gap": due["materialization_gap"],
        "confirmation_gap": due["confirmation_gap"],
        "remote_fact_gap": due["remote_fact_gap"],
        "target_deficit_count": due["target_deficit_count"],
        "targets": due["targets"],
        "source_state": due["source_state"],
        "source_message_count": due["source_message_count"],
        "capacity_warning": str(task_stats.get("capacity_warning") or ""),
        "unique_account_capacity_shortfall": dict(
            task_stats.get("channel_view_unique_account_capacity_shortfall") or {}
        ),
        "target_per_message": int(task_stats.get("target_per_message") or 0),
        "max_effective_per_message": int(
            task_stats.get("max_effective_per_message") or 0
        ),
        "confirmed_count": due["confirmed_count"],
        "remote_fact_count": due["remote_fact_count"],
        "post_release_remote_fact_count": due["post_release_remote_fact_count"],
    }


def _view_due_snapshot(
    session,
    task: Task,
    ledger: TaskDayLedger,
    since: datetime | None = None,
) -> dict[str, Any]:
    config = dict(task.type_config or {})
    targets = list(session.scalars(select(ChannelViewDailyMessageTarget).where(
        ChannelViewDailyMessageTarget.task_day_ledger_id == ledger.id,
    )))
    rows = _view_target_rows(session, task, ledger, targets, since)
    return {
        "expected_due_count": sum(row["due_count"] for row in rows),
        "materialized_count": sum(row["materialized_count"] for row in rows),
        "materialization_gap": sum(row["materialization_gap"] for row in rows),
        "confirmed_count": sum(row["confirmed_count"] for row in rows),
        "confirmation_gap": sum(row["confirmation_gap"] for row in rows),
        "remote_fact_count": sum(row["remote_fact_count"] for row in rows),
        "remote_fact_gap": sum(row["remote_fact_gap"] for row in rows),
        "post_release_remote_fact_count": sum(
            row["post_release_remote_fact_count"] for row in rows
        ),
        "target_deficit_count": sum(
            1 for row in rows if row["materialization_gap"] or row["confirmation_gap"]
        ),
        "targets": rows,
        "source_message_count": len(targets),
        "source_state": _view_source_state(task, bool(targets), config),
    }


def _view_target_rows(
    session,
    task: Task,
    ledger: TaskDayLedger,
    targets: list[ChannelViewDailyMessageTarget],
    since: datetime | None,
) -> list[dict[str, Any]]:
    messages = target_messages(session, {target.channel_message_id: target for target in targets})
    materialized = view_materialized_account_ids_for_messages(
        session,
        ledger,
        messages,
    )
    confirmed = _view_confirmed_status_counts(session, ledger)
    facts = _view_fact_counts(session, ledger, targets, since=since)
    now_value = datetime.now(tz=BEIJING)
    rows: list[dict[str, Any]] = []
    for target in targets:
        message_id = int(target.channel_message_id)
        due = max(
            int(target.due_count or 0),
            channel_view_target_due(
                target,
                ledger,
                task.pacing_config or {},
                now=now_value,
            ),
        )
        fact_count, post_release_count = facts.get(message_id, (0, 0))
        attach_baseline = int(target.ledger_confirmed_at_attach or 0)
        materialized_count = max(
            0,
            len(materialized.get(message_id, set())) - attach_baseline,
        )
        confirmed_count = max(0, confirmed.get(message_id, 0) - attach_baseline)
        fact_count = max(0, fact_count - attach_baseline)
        rows.append({
            "channel_message_id": message_id,
            "due_count": due,
            "materialized_count": materialized_count,
            "materialization_gap": max(0, due - materialized_count),
            "confirmed_count": confirmed_count,
            "confirmation_gap": max(0, due - confirmed_count),
            "remote_fact_count": fact_count,
            "remote_fact_gap": max(0, confirmed_count - fact_count),
            "post_release_remote_fact_count": post_release_count,
            "source_state": target.source_state,
        })
    return rows


def _view_confirmed_status_counts(session, ledger: TaskDayLedger) -> dict[int, int]:
    rows = session.execute(
        select(
            ViewFulfillmentObligation.channel_message_id,
            func.count(ViewFulfillmentObligation.id),
        )
        .where(
            ViewFulfillmentObligation.task_day_ledger_id == ledger.id,
            ViewFulfillmentObligation.status == "confirmed",
        )
        .group_by(ViewFulfillmentObligation.channel_message_id)
    )
    return {int(message_id): int(count) for message_id, count in rows}


def _view_fact_counts(
    session,
    ledger: TaskDayLedger,
    targets: list[ChannelViewDailyMessageTarget],
    *,
    since: datetime | None,
) -> dict[int, tuple[int, int]]:
    if not targets:
        return {}
    post_release = _view_post_release_case(since)
    rows = session.execute(
        select(
            ChannelViewDailyMessageTarget.channel_message_id,
            func.count(ViewRemoteFact.id),
            func.coalesce(func.sum(post_release), 0),
        )
        .select_from(ChannelViewDailyMessageTarget)
        .join(
            ViewFulfillmentObligation,
            and_(
                ViewFulfillmentObligation.task_day_ledger_id
                == ChannelViewDailyMessageTarget.task_day_ledger_id,
                ViewFulfillmentObligation.channel_message_id
                == ChannelViewDailyMessageTarget.channel_message_id,
            ),
        )
        .join(ViewRemoteFact, ViewRemoteFact.obligation_id == ViewFulfillmentObligation.id)
        .where(ChannelViewDailyMessageTarget.task_day_ledger_id == ledger.id)
        .group_by(ChannelViewDailyMessageTarget.channel_message_id)
    )
    return {
        int(message_id): (int(count), int(post_count))
        for message_id, count, post_count in rows
    }


def _view_post_release_case(since: datetime | None):
    conditions = [
        ViewRemoteFact.remote_confirmed_at
        >= ChannelViewDailyMessageTarget.created_at,
    ]
    if since is not None:
        conditions.append(ViewRemoteFact.remote_confirmed_at >= since)
    return case((and_(*conditions), 1), else_=0)


def _view_source_state(task: Task, has_targets: bool, config: dict) -> str:
    if has_targets:
        return "active"
    if str(task.last_error or "").startswith("采集频道消息失败"):
        return "listener_stalled"
    if config.get("listen_new_messages") is False:
        return "source_empty_terminal"
    return "waiting_for_source"


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
        snapshot["group_daily"] = _group_daily_snapshot(session, ledger, since=since)
        snapshot["group_runtime"] = _group_runtime_snapshot(session, ledger)
    elif task.type == "search_click":
        snapshot["search_click"] = _search_snapshot(session, ledger, since)
        snapshot["search_runtime"] = _search_runtime_snapshot(session, ledger)
    elif task.type == "channel_view":
        snapshot["channel_view"] = _view_snapshot(session, task, ledger, since)
        snapshot["channel_view_messages"] = _view_message_snapshot(session, ledger)
        snapshot["view_runtime"] = view_open_details(session, ledger)
    return snapshot


def _base_task_snapshot(session, task: Task, ledger: TaskDayLedger | None, since: datetime) -> dict[str, Any]:
    return {
        "task_id": task.id,
        "name": task.name,
        "task_type": task.type,
        "task_status": task.status,
        "task_deleted": task.deleted_at is not None,
        "last_error": task.last_error,
        "next_run_at": iso(task.next_run_at),
        "search_click_runtime_blocker": (
            (task.stats or {}).get("search_click_runtime_blocker")
        ),
        "projected_eligible_attempt_capacity": (
            (task.stats or {}).get("projected_eligible_attempt_capacity")
        ),
        "ledger_id": ledger.id if ledger else None,
        "ledger_day_phase": ledger.day_phase if ledger else None,
        "ledger_local_date": str(ledger.obligation_local_date) if ledger else None,
        "ledger_deadline_at": iso(ledger.deadline_at) if ledger else None,
        "planner_runtime_error": _planner_error_after(task, since),
        "post_release_action_counts": _action_counts(session, task, since),
        "attempts": _attempt_snapshot(session, task, since),
    }


def main() -> None:
    since = parse_release_since()
    rows: list[dict[str, Any]] = []
    with SessionLocal() as session:
        configure_readonly_snapshot(session)
        task_ids = parse_task_ids(session)
        if not task_ids:
            raise ValueError("no active channel_view tasks discovered")
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

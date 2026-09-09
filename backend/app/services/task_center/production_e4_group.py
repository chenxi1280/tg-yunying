"""Read-only group coverage and typed message evidence for production reports."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import and_, case, func, select
from app.models import (
    Action, ExecutionAttempt, FulfillmentRemoteFact, TaskAccountDailyCoverage,
    Task, TaskDayLedger, TaskGroupDailyMessageSlot, TaskGroupDailyTarget,
)
from .production_e4_diagnostics import ai_open_action_details

UNIFIED_CONTRACT = "unified_engagement_v1"


def _group_daily_snapshot(session, ledger: TaskDayLedger, *, since: datetime | None = None) -> dict[str, int]:
    target = session.execute(
        select(
            func.count(TaskGroupDailyTarget.id),
            func.coalesce(func.sum(TaskGroupDailyTarget.due_message_count), 0),
            func.coalesce(func.sum(TaskGroupDailyTarget.confirmed_message_count), 0),
        ).where(
            TaskGroupDailyTarget.task_day_ledger_id == ledger.id,
            TaskGroupDailyTarget.tenant_id == ledger.tenant_id,
            TaskGroupDailyTarget.task_id == ledger.task_id,
        )
    ).one()
    coverage = session.execute(
        select(
            func.count(TaskAccountDailyCoverage.id),
            func.coalesce(func.sum(_coverage_confirmed_case()), 0),
            func.coalesce(func.sum(case((TaskAccountDailyCoverage.state == "abandoned_for_day", 1), else_=0)), 0),
        ).where(*_coverage_scope(ledger))
    ).one()
    return {
        "target_row_count": int(target[0]),
        "due_message_count": int(target[1]),
        "confirmed_message_count": int(target[2]),
        "coverage_required_count": _coverage_required_count(session, ledger, coverage),
        "coverage_total_count": int(coverage[0]),
        "coverage_confirmed_count": int(coverage[1]),
        "coverage_active_count": int(coverage[0]) - int(coverage[2]),
        "coverage_abandoned_count": int(coverage[2]),
        "post_release_remote_fact_count": _post_release_message_facts(session, ledger, since=since),
    }


def _coverage_required_count(session, ledger, counts):
    task = session.get(Task, ledger.task_id)
    if task is not None and (task.type_config or {}).get("engagement_contract_version") == UNIFIED_CONTRACT:
        return int(counts[0])
    return int(counts[0]) - int(counts[2])


def _group_runtime_snapshot(session, ledger: TaskDayLedger) -> dict[str, Any]:
    coverage_rows = session.execute(
        select(
            TaskAccountDailyCoverage.state,
            TaskAccountDailyCoverage.blocker_code,
            func.count(TaskAccountDailyCoverage.id),
            func.count(func.distinct(TaskAccountDailyCoverage.account_id)),
        )
        .where(*_coverage_scope(ledger))
        .group_by(TaskAccountDailyCoverage.state, TaskAccountDailyCoverage.blocker_code)
    )
    slot_rows = session.execute(
        select(
            TaskGroupDailyMessageSlot.slot_kind,
            TaskGroupDailyMessageSlot.state,
            func.count(TaskGroupDailyMessageSlot.id),
        )
        .where(
            TaskGroupDailyMessageSlot.task_day_ledger_id == ledger.id,
            TaskGroupDailyMessageSlot.tenant_id == ledger.tenant_id,
        )
        .group_by(TaskGroupDailyMessageSlot.slot_kind, TaskGroupDailyMessageSlot.state)
    )
    actions = list(session.scalars(select(Action).where(
        Action.task_id == ledger.task_id,
        Action.tenant_id == ledger.tenant_id,
        Action.action_type == "send_message",
        Action.payload["task_day_ledger_id"].as_string() == ledger.id,
        Action.status.in_(("pending", "claiming", "executing")),
    )))
    return {
        "coverage_distinct_account_count": int(session.scalar(select(
            func.count(func.distinct(TaskAccountDailyCoverage.account_id)),
        ).where(*_coverage_scope(ledger))) or 0),
        "open_action_counts": _generation_counts(actions),
        **ai_open_action_details(session, ledger, actions),
        "coverage_counts": [
            {"state": state, "blocker_code": blocker or "", "count": int(count),
             "distinct_account_count": int(accounts)}
            for state, blocker, count, accounts in coverage_rows
        ],
        "quantity_slot_counts": [
            {"slot_kind": kind, "state": state, "count": int(count)}
            for kind, state, count in slot_rows
        ],
    }


def _generation_counts(actions):
    counts: dict[str, int] = {}
    for action in actions:
        payload = action.payload or {}
        content_state = "ready" if str(payload.get("message_text") or "").strip() else "empty"
        key = f"{action.status}:{payload.get('ai_generation_status') or ''}:{content_state}"
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def _coverage_confirmed_case():
    return case(
        (
            and_(
                TaskAccountDailyCoverage.state == "confirmed",
                TaskAccountDailyCoverage.target_count > 0,
                TaskAccountDailyCoverage.confirmed_count >= TaskAccountDailyCoverage.target_count,
                func.trim(TaskAccountDailyCoverage.last_remote_message_id) != "",
            ),
            1,
        ),
        else_=0,
    )


def _coverage_scope(ledger):
    return (
        TaskAccountDailyCoverage.tenant_id == ledger.tenant_id,
        TaskAccountDailyCoverage.task_id == ledger.task_id,
        TaskAccountDailyCoverage.task_day_ledger_id == ledger.id,
    )


def _post_release_message_facts(session, ledger, *, since):
    if since is None:
        return 0
    fact, attempt, action = FulfillmentRemoteFact, ExecutionAttempt, Action
    return int(session.scalar(
        select(func.count(func.distinct(fact.action_id)))
        .select_from(fact)
        .join(attempt, attempt.id == fact.attempt_id)
        .join(action, and_(action.id == fact.action_id, action.id == attempt.action_id))
        .where(
            fact.tenant_id == ledger.tenant_id,
            fact.task_id == ledger.task_id,
            fact.task_day_ledger_id == ledger.id,
            fact.task_type == "group_ai_chat",
            fact.fact_kind == "remote_message_observed",
            fact.mutation_kind == "send_message",
            fact.observed_at >= since,
            fact.observed_at >= attempt.gateway_call_started_at,
            fact.outcome["remote_message_id"].as_string() == attempt.remote_message_id,
            action.tenant_id == ledger.tenant_id,
            action.task_id == ledger.task_id,
            action.task_type == "group_ai_chat",
            action.action_type == "send_message",
            action.payload["task_day_ledger_id"].as_string() == ledger.id,
            attempt.tenant_id == ledger.tenant_id,
            attempt.account_id == action.account_id,
            attempt.status == "success",
            func.trim(attempt.remote_message_id) != "",
            attempt.gateway_call_started_at >= since,
        )
    ) or 0)

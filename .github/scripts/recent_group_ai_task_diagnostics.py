from __future__ import annotations

import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import text

from app.database import SessionLocal


LOCAL_TIMEZONE = ZoneInfo("Asia/Shanghai")
DEFAULT_TASK_LIMIT = 20


def _rows(session, statement: str, **params) -> list[dict]:
    return [
        dict(row)
        for row in session.execute(text(statement), params).mappings()
    ]


def _recent_running_tasks(session, task_limit: int) -> list[dict]:
    return _rows(
        session,
        """
        SELECT id, tenant_id, name, status, fulfillment_contract_version,
               created_at, updated_at, next_run_at, last_error
        FROM tasks
        WHERE deleted_at IS NULL
          AND type = 'group_ai_chat'
          AND status = 'running'
        ORDER BY created_at DESC, id DESC
        LIMIT :task_limit
        """,
        task_limit=task_limit,
    )


def _ledger(session, task_id: str) -> dict | None:
    rows = _rows(
        session,
        """
        SELECT id, obligation_local_date, period_start_at, deadline_at,
               day_phase, lifecycle_status
        FROM task_day_ledgers
        WHERE task_id = :task_id
        ORDER BY period_start_at DESC, id DESC
        LIMIT 1
        """,
        task_id=task_id,
    )
    return rows[0] if rows else None


def _daily_state(session, task_id: str, ledger: dict) -> dict:
    rows = _rows(
        session,
        """
        SELECT COALESCE(SUM(due_message_count), 0) AS due_count,
               COALESCE(SUM(confirmed_message_count), 0) AS confirmed_count,
               COALESCE(SUM(coverage_confirmed_account_count), 0)
                   AS coverage_confirmed_count,
               COALESCE(SUM(frozen_account_count), 0)
                   AS coverage_required_count
        FROM task_group_daily_targets
        WHERE task_id = :task_id
          AND target_date = :local_date
        """,
        task_id=task_id,
        local_date=ledger["obligation_local_date"],
    )
    return rows[0]


def _action_state(
    session, task_id: str, ledger: dict, release_live_at: datetime
) -> list[dict]:
    return _rows(
        session,
        """
        SELECT status,
               COALESCE(payload ->> 'ai_generation_status', '')
                   AS generation_status,
               COUNT(*) AS action_count,
               COUNT(*) FILTER (WHERE created_at >= :release_live_at)
                   AS post_release_count,
               MAX(created_at) AS latest_created_at
        FROM actions
        WHERE task_id = :task_id
          AND action_type = 'send_message'
          AND created_at >= :period_start_at
        GROUP BY status, generation_status
        ORDER BY status, generation_status
        """,
        task_id=task_id,
        period_start_at=ledger["period_start_at"],
        release_live_at=release_live_at,
    )


def _generation_state(session, task_id: str, ledger: dict) -> list[dict]:
    return _rows(
        session,
        """
        SELECT state, COUNT(*) AS job_count,
               MAX(created_at) AS latest_created_at
        FROM generation_jobs
        WHERE task_id = :task_id
          AND created_at >= :period_start_at
        GROUP BY state
        ORDER BY state
        """,
        task_id=task_id,
        period_start_at=ledger["period_start_at"],
    )


def _generation_failure_state(
    session, task_id: str, release_live_at: datetime
) -> list[dict]:
    return _rows(
        session,
        """
        SELECT COALESCE(result ->> 'error_code', '') AS error_code,
               COALESCE(result ->> 'generation_stage', '') AS generation_stage,
               LEFT(COALESCE(result ->> 'error_message', ''), 500) AS error_message,
               COUNT(*) AS action_count,
               MIN(created_at) AS first_created_at,
               MAX(created_at) AS latest_created_at
        FROM actions
        WHERE task_id = :task_id
          AND action_type = 'send_message'
          AND status = 'failed'
          AND payload ->> 'ai_generation_status' = 'ai_generation_failed'
          AND created_at >= :release_live_at
        GROUP BY error_code, generation_stage, error_message
        ORDER BY action_count DESC, error_code, generation_stage, error_message
        LIMIT 20
        """,
        task_id=task_id,
        release_live_at=release_live_at,
    )


def _remote_state(
    session, task_id: str, ledger: dict, release_live_at: datetime
) -> dict:
    rows = _rows(
        session,
        """
        SELECT COUNT(DISTINCT e.id) AS attempt_count,
               COUNT(DISTINCT e.id) FILTER (
                   WHERE e.gateway_call_started_at IS NOT NULL
               ) AS gateway_started_count,
               COUNT(DISTINCT e.id) FILTER (
                   WHERE COALESCE(e.remote_message_id, '') <> ''
               ) AS remote_message_count,
               COUNT(DISTINCT e.id) FILTER (
                   WHERE COALESCE(e.remote_message_id, '') <> ''
                     AND e.after_call_at >= :release_live_at
               ) AS post_release_remote_message_count,
               MAX(e.after_call_at) FILTER (
                   WHERE COALESCE(e.remote_message_id, '') <> ''
               ) AS latest_remote_message_at,
               COUNT(DISTINCT f.fact_id) AS typed_remote_fact_count,
               MAX(f.observed_at) AS latest_typed_fact_at
        FROM actions a
        LEFT JOIN execution_attempts e ON e.action_id = a.id
        LEFT JOIN fulfillment_remote_facts f ON f.action_id = a.id
        WHERE a.task_id = :task_id
          AND a.action_type = 'send_message'
          AND a.created_at >= :period_start_at
        """,
        task_id=task_id,
        period_start_at=ledger["period_start_at"],
        release_live_at=release_live_at,
    )
    return rows[0]


def classify_first_boundary(snapshot: dict) -> str:
    if snapshot["ledger"] is None:
        return "ledger_missing"
    daily = snapshot["daily"]
    if int(daily["due_count"]) <= int(daily["confirmed_count"]):
        return "no_due_gap"
    actions = snapshot["actions"]
    if not actions:
        return "planner_materialization"
    open_rows = [row for row in actions if row["status"] in {"pending", "executing"}]
    if any(row["generation_status"] in {"pending", "generating"} for row in open_rows):
        return "generation"
    remote = snapshot["remote"]
    if int(remote["attempt_count"]) == 0:
        return "dispatcher_pre_gateway"
    if int(remote["remote_message_count"]) == 0:
        return "gateway_remote_result"
    return "active_remote_sending"


def _snapshot(session, task: dict, release_live_at: datetime) -> dict:
    ledger = _ledger(session, task["id"])
    snapshot = {"task": task, "ledger": ledger}
    if ledger is None:
        snapshot.update(
            {
                "daily": {},
                "actions": [],
                "generation_jobs": [],
                "generation_failures": [],
                "remote": {},
            }
        )
    else:
        snapshot.update(
            {
                "daily": _daily_state(session, task["id"], ledger),
                "actions": _action_state(session, task["id"], ledger, release_live_at),
                "generation_jobs": _generation_state(session, task["id"], ledger),
                "generation_failures": _generation_failure_state(
                    session, task["id"], release_live_at
                ),
                "remote": _remote_state(session, task["id"], ledger, release_live_at),
            }
        )
    snapshot["first_boundary"] = classify_first_boundary(snapshot)
    return snapshot


def parse_release_live_at(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is not None:
        return parsed
    return parsed.replace(tzinfo=LOCAL_TIMEZONE)


def main() -> int:
    release_live_at = parse_release_live_at(
        os.environ["TASK_FULFILLMENT_RELEASE_LIVE_AT"]
    )
    task_limit = int(os.environ.get("RECENT_GROUP_AI_TASK_LIMIT", DEFAULT_TASK_LIMIT))
    captured_at = datetime.now(LOCAL_TIMEZONE)
    with SessionLocal() as session:
        tasks = _recent_running_tasks(session, task_limit)
        snapshots = [_snapshot(session, task, release_live_at) for task in tasks]
    for snapshot in snapshots:
        print("RECENT_GROUP_AI_TASK=" + json.dumps(snapshot, ensure_ascii=False, default=str))
    print(
        "RECENT_GROUP_AI_SUMMARY="
        + json.dumps(
            {"captured_at": captured_at, "row_count": len(snapshots)},
            ensure_ascii=False,
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

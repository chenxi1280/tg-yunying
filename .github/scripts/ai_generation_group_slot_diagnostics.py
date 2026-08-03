from __future__ import annotations

import json
import os
from datetime import datetime

from sqlalchemy import text

from app.database import SessionLocal


DEFAULT_TASK_IDS = (
    "84180c47-cf92-408e-bdd9-c68804f7de29",
    "7805d8f2-dfef-4842-b11c-14cc1f434c94",
)
GROUP_SLOT_QUERY = text("""
    WITH target_groups AS (
        SELECT DISTINCT (a.payload ->> 'group_id')::bigint AS group_id
        FROM actions a
        WHERE a.task_id = ANY(:task_ids)
          AND a.action_type = 'send_message'
          AND COALESCE(a.payload ->> 'group_id', '') ~ '^[0-9]+$'
    )
    SELECT a.id AS action_id,
           a.task_id,
           t.name AS task_name,
           t.status AS task_status,
           t.deleted_at AS task_deleted_at,
           (a.payload ->> 'group_id')::bigint AS group_id,
           a.account_id,
           a.status,
           a.scheduled_at,
           a.created_at,
           COALESCE(a.payload ->> 'ai_generation_status', '') AS generation_status,
           LENGTH(COALESCE(a.payload ->> 'message_text', '')) > 0 AS text_present,
           COALESCE(a.payload ->> 'group_bot_admission_state', '') AS admission_state,
           COALESCE(a.result ->> 'error_code', '') AS error_code,
           COALESCE(a.result ->> 'validation_stage', '') AS validation_stage,
           COALESCE(a.claim_owner, '') <> '' AS has_claim,
           a.claim_expires_at,
           CASE
             WHEN a.status = 'executing'
                  AND COALESCE(a.payload ->> 'ai_generation_status', '') = 'generating'
               THEN 'generating_occupant'
             WHEN a.status IN ('pending', 'claiming', 'executing')
                  AND COALESCE(a.payload ->> 'ai_generation_status', '') = 'ready'
                  AND LENGTH(COALESCE(a.payload ->> 'message_text', '')) > 0
               THEN 'ready_occupant'
             ELSE 'open_candidate'
           END AS generation_role
    FROM actions a
    JOIN tasks t ON t.id = a.task_id
    JOIN target_groups g
      ON COALESCE(a.payload ->> 'group_id', '') ~ '^[0-9]+$'
     AND (a.payload ->> 'group_id')::bigint = g.group_id
    WHERE a.task_type = 'group_ai_chat'
      AND a.action_type = 'send_message'
      AND a.status IN ('pending', 'claiming', 'executing')
    ORDER BY group_id, generation_role DESC, a.scheduled_at, a.created_at, a.id
    LIMIT 200
""")

ELIGIBILITY_QUERY = text("""
    WITH candidates AS (
        SELECT a.id AS action_id,
               a.task_id,
               (a.payload ->> 'group_id')::bigint AS group_id,
               a.account_id,
               a.status = 'pending' AS action_pending,
               a.account_id IS NOT NULL AS account_present,
               a.scheduled_at <= NOW() + INTERVAL '30 minutes' AS within_lookahead,
               t.status = 'running' AS task_running,
               t.deleted_at IS NULL AS task_not_deleted,
               COALESCE(a.payload ->> 'ai_generation_status', '')
                   IN ('pending', 'ai_result_persist_unknown') AS generation_pending,
               LENGTH(COALESCE(a.payload ->> 'message_text', '')) = 0 AS text_empty,
               EXISTS (
                   SELECT 1
                   FROM actions busy
                   WHERE busy.account_id = a.account_id
                     AND busy.status = 'executing'
               ) AS account_busy,
               EXISTS (
                   SELECT 1
                   FROM actions occupied
                   WHERE occupied.id <> a.id
                     AND occupied.tenant_id = a.tenant_id
                     AND occupied.task_type = 'group_ai_chat'
                     AND occupied.action_type = 'send_message'
                     AND occupied.payload ->> 'group_id' = a.payload ->> 'group_id'
                     AND (
                         (occupied.status = 'executing'
                          AND occupied.payload ->> 'ai_generation_status' = 'generating')
                         OR
                         (occupied.status IN ('pending', 'claiming', 'executing')
                          AND occupied.payload ->> 'ai_generation_status' = 'ready'
                          AND LENGTH(COALESCE(occupied.payload ->> 'message_text', '')) > 0)
                     )
               ) AS group_busy
        FROM actions a
        JOIN tasks t ON t.id = a.task_id
        WHERE a.task_id = ANY(:task_ids)
          AND a.task_type = 'group_ai_chat'
          AND a.action_type = 'send_message'
          AND COALESCE(a.payload ->> 'group_id', '') ~ '^[0-9]+$'
    )
    SELECT *,
           action_pending AND account_present AND within_lookahead
             AND task_running AND task_not_deleted AND generation_pending
             AND text_empty AND NOT account_busy AND NOT group_busy AS eligible
    FROM candidates
    ORDER BY task_id, group_id, action_id
""")

HEARTBEAT_QUERY = text("""
    SELECT worker_id, hostname, pid, status, started_at, last_seen_at,
           heartbeat_metadata
    FROM worker_heartbeats
    WHERE process_type = 'ai-generation'
    ORDER BY last_seen_at DESC
    LIMIT 20
""")

ACCOUNT_OCCUPANT_QUERY = text("""
    WITH target_accounts AS (
        SELECT DISTINCT account_id
        FROM actions
        WHERE task_id = ANY(:task_ids)
          AND account_id IS NOT NULL
    )
    SELECT a.id AS action_id, a.task_id, a.account_id, a.task_type,
           a.action_type, a.status, a.scheduled_at, a.lease_expires_at,
           COALESCE(a.claim_owner, '') AS claim_owner,
           COALESCE(a.lease_owner, '') AS lease_owner,
           COALESCE(a.payload ->> 'ai_generation_status', '') AS generation_status,
           COALESCE(a.result ->> 'error_code', '') AS error_code
    FROM actions a
    JOIN target_accounts target ON target.account_id = a.account_id
    WHERE a.status = 'executing'
    ORDER BY a.account_id, a.scheduled_at, a.id
    LIMIT 300
""")


def _task_ids() -> tuple[str, ...]:
    raw = os.getenv("AI_GENERATION_DIAGNOSTIC_TASK_IDS", "")
    values = tuple(value.strip() for value in raw.split(",") if value.strip())
    return values or DEFAULT_TASK_IDS


def _json_value(value):
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _rows(session, task_ids: tuple[str, ...]) -> list[dict]:
    result = session.execute(GROUP_SLOT_QUERY, {"task_ids": list(task_ids)})
    return [
        {key: _json_value(value) for key, value in row.items()}
        for row in result.mappings()
    ]


def _query_rows(session, query, task_ids: tuple[str, ...] | None = None) -> list[dict]:
    params = {"task_ids": list(task_ids)} if task_ids else {}
    result = session.execute(query, params)
    return [
        {key: _json_value(value) for key, value in row.items()}
        for row in result.mappings()
    ]


def _eligibility_summary(rows: list[dict]) -> dict:
    keys = (
        "eligible",
        "account_busy",
        "group_busy",
        "action_pending",
        "within_lookahead",
        "task_running",
        "task_not_deleted",
        "generation_pending",
        "text_empty",
    )
    return {
        key: sum(1 for row in rows if bool(row[key]))
        for key in keys
    }


def _print_rows(prefix: str, rows: list[dict]) -> None:
    for row in rows:
        print(prefix + json.dumps(row, ensure_ascii=False, sort_keys=True))


def main() -> None:
    task_ids = _task_ids()
    with SessionLocal() as session:
        rows = _rows(session, task_ids)
        eligibility_rows = _query_rows(session, ELIGIBILITY_QUERY, task_ids)
        heartbeat_rows = _query_rows(session, HEARTBEAT_QUERY)
        occupant_rows = _query_rows(session, ACCOUNT_OCCUPANT_QUERY, task_ids)
    role_counts: dict[str, int] = {}
    for row in rows:
        key = f"{row['group_id']}:{row['generation_role']}"
        role_counts[key] = role_counts.get(key, 0) + 1
    print("AI_GENERATION_GROUP_SLOT_SUMMARY=" + json.dumps(
        {"task_ids": task_ids, "role_counts": role_counts, "row_count": len(rows)},
        ensure_ascii=False,
        sort_keys=True,
    ))
    _print_rows("AI_GENERATION_GROUP_SLOT=", rows)
    print("AI_GENERATION_ELIGIBILITY_SUMMARY=" + json.dumps(
        _eligibility_summary(eligibility_rows),
        ensure_ascii=False,
        sort_keys=True,
    ))
    _print_rows("AI_GENERATION_ELIGIBILITY=", eligibility_rows)
    _print_rows("AI_GENERATION_HEARTBEAT=", heartbeat_rows)
    _print_rows("AI_GENERATION_ACCOUNT_OCCUPANT=", occupant_rows)


if __name__ == "__main__":
    main()

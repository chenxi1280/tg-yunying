from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal

from sqlalchemy import text

from app.database import SessionLocal


AI_CLAIM_QUERY = text("""
    SELECT id AS action_id, task_id, account_id, status,
           scheduled_at, lease_expires_at,
           COALESCE(claim_owner, '') AS claim_owner,
           COALESCE(lease_owner, '') AS lease_owner,
           COALESCE(payload ->> 'group_id', '') AS group_id,
           COALESCE(payload ->> 'ai_generation_status', '') AS generation_status
    FROM actions
    WHERE status = 'executing'
      AND (claim_owner LIKE 'ai-generation:%'
           OR lease_owner LIKE 'ai-generation:%')
    ORDER BY scheduled_at, id
    LIMIT 100
""")

DATABASE_ACTIVITY_QUERY = text("""
    SELECT pid, state, application_name,
           ROUND(EXTRACT(EPOCH FROM (NOW() - query_start))::numeric, 1) AS query_age_seconds,
           ROUND(EXTRACT(EPOCH FROM (NOW() - xact_start))::numeric, 1) AS transaction_age_seconds,
           wait_event_type, wait_event,
           pg_blocking_pids(pid) AS blocking_pids,
           MD5(query) AS query_fingerprint,
           CASE
             WHEN query ~* '^\\s*SELECT' THEN 'select'
             WHEN query ~* '^\\s*UPDATE' THEN 'update'
             WHEN query ~* '^\\s*INSERT' THEN 'insert'
             WHEN query ~* '^\\s*DELETE' THEN 'delete'
             ELSE 'other'
           END AS query_verb,
           ARRAY_REMOVE(ARRAY[
             CASE WHEN query ~* '\\mactions\\M' THEN 'actions' END,
             CASE WHEN query ~* '\\mgeneration_jobs\\M' THEN 'generation_jobs' END,
             CASE WHEN query ~* '\\mfulfillment_obligation_projections\\M'
                  THEN 'fulfillment_obligation_projections' END,
             CASE WHEN query ~* '\\mtasks\\M' THEN 'tasks' END,
             CASE WHEN query ~* '\\mworker_heartbeats\\M' THEN 'worker_heartbeats' END
           ], NULL) AS query_relations,
           CASE
             WHEN query LIKE '%ai_generation_status%'
                  AND query LIKE '%actions_2%' THEN 'ai_generation_claim'
             WHEN query ~* '^\\s*UPDATE\\s+actions' THEN 'action_update'
             WHEN query ~* '^\\s*INSERT\\s+INTO\\s+generation_jobs' THEN 'generation_job_insert'
             WHEN query ~* '^\\s*UPDATE\\s+generation_jobs' THEN 'generation_job_update'
             WHEN query ~* '^\\s*SELECT' AND query LIKE '%generation_jobs%' THEN 'generation_job_select'
             WHEN query LIKE '%worker_heartbeats%' THEN 'worker_heartbeat'
             WHEN query LIKE '%actions%' THEN 'other_action_query'
             ELSE 'other'
           END AS query_kind
    FROM pg_stat_activity
    WHERE datname = current_database()
      AND pid <> pg_backend_pid()
      AND state <> 'idle'
    ORDER BY query_start
    LIMIT 100
""")

BLOCKING_EDGE_QUERY = text("""
    SELECT blocked.pid AS blocked_pid,
           blocker.pid AS blocking_pid,
           blocked.application_name AS blocked_application_name,
           blocker.application_name AS blocking_application_name,
           ROUND(EXTRACT(EPOCH FROM (NOW() - blocked.xact_start))::numeric, 1)
               AS blocked_transaction_age_seconds,
           ROUND(EXTRACT(EPOCH FROM (NOW() - blocker.xact_start))::numeric, 1)
               AS blocking_transaction_age_seconds,
           blocked.wait_event_type, blocked.wait_event,
           MD5(blocked.query) AS blocked_query_fingerprint,
           MD5(blocker.query) AS blocking_query_fingerprint,
           blocker.state AS blocking_state
    FROM pg_stat_activity AS blocked
    CROSS JOIN LATERAL unnest(pg_blocking_pids(blocked.pid)) AS edge(blocking_pid)
    JOIN pg_stat_activity AS blocker ON blocker.pid = edge.blocking_pid
    WHERE blocked.datname = current_database()
    ORDER BY blocked.xact_start, blocked.pid, blocker.pid
    LIMIT 100
""")

BLOCKING_LOCK_QUERY = text("""
    SELECT activity.pid,
           MD5(activity.query) AS query_fingerprint,
           lock.mode,
           lock.granted,
           COALESCE(relation.relname, '') AS relation_name,
           lock.locktype
    FROM pg_stat_activity AS activity
    JOIN pg_locks AS lock ON lock.pid = activity.pid
    LEFT JOIN pg_class AS relation ON relation.oid = lock.relation
    WHERE activity.datname = current_database()
      AND (
        cardinality(pg_blocking_pids(activity.pid)) > 0
        OR activity.pid IN (
          SELECT unnest(pg_blocking_pids(blocked.pid))
          FROM pg_stat_activity AS blocked
          WHERE blocked.datname = current_database()
        )
      )
      AND (lock.locktype <> 'relation' OR lock.mode <> 'AccessShareLock')
    ORDER BY activity.pid, lock.granted DESC, relation.relname, lock.locktype, lock.mode
    LIMIT 300
""")

RUNTIME_SCOPE_QUERY = text("""
    SELECT dispatcher_scope, contract_activation_state,
           active_contract_version, candidate_contract_version,
           runtime_shard_total, topology_fingerprint,
           capacity_config_fingerprint, updated_at
    FROM dispatch_claim_scopes
    ORDER BY updated_at DESC
    LIMIT 10
""")

TASK_BACKLOG_QUERY = text("""
    SELECT task.id AS task_id, task.name AS task_name,
           COUNT(action.id) FILTER (
             WHERE action.status = 'pending'
               AND COALESCE(action.payload ->> 'ai_generation_status', '')
                   IN ('pending', 'ai_result_persist_unknown')
               AND COALESCE(action.payload ->> 'message_text', '') = ''
           ) AS generation_pending_total,
           COUNT(action.id) FILTER (
             WHERE action.status = 'pending'
               AND COALESCE(action.payload ->> 'ai_generation_status', '')
                   IN ('pending', 'ai_result_persist_unknown')
               AND COALESCE(action.payload ->> 'message_text', '') = ''
               AND action.scheduled_at <= NOW()
           ) AS generation_overdue_now,
           COUNT(action.id) FILTER (
             WHERE action.status = 'pending'
               AND COALESCE(action.payload ->> 'ai_generation_status', '')
                   IN ('pending', 'ai_result_persist_unknown')
               AND COALESCE(action.payload ->> 'message_text', '') = ''
               AND action.scheduled_at <= NOW() + INTERVAL '30 minutes'
           ) AS generation_eligible_lookahead,
           COUNT(action.id) FILTER (
             WHERE action.status = 'pending'
               AND COALESCE(action.payload ->> 'ai_generation_status', '')
                   IN ('pending', 'ai_result_persist_unknown')
               AND COALESCE(action.payload ->> 'message_text', '') = ''
               AND action.scheduled_at > NOW() + INTERVAL '30 minutes'
           ) AS generation_future_after_lookahead,
           COUNT(action.id) FILTER (
             WHERE action.status = 'pending'
               AND action.payload ->> 'ai_generation_status' = 'ready'
               AND COALESCE(action.payload ->> 'message_text', '') <> ''
           ) AS ready_pending_total,
           COUNT(action.id) FILTER (
             WHERE action.status = 'pending'
               AND action.payload ->> 'ai_generation_status' = 'ready'
               AND COALESCE(action.payload ->> 'message_text', '') <> ''
               AND action.scheduled_at <= NOW()
           ) AS ready_overdue_now,
           COUNT(action.id) FILTER (
             WHERE action.status = 'executing'
               AND action.payload ->> 'ai_generation_status' = 'generating'
           ) AS generation_executing,
           MIN(action.scheduled_at) FILTER (
             WHERE action.status = 'pending'
               AND COALESCE(action.payload ->> 'ai_generation_status', '')
                   IN ('pending', 'ai_result_persist_unknown')
           ) AS oldest_generation_pending_at,
           MIN(action.scheduled_at) FILTER (
             WHERE action.status = 'pending'
               AND action.payload ->> 'ai_generation_status' = 'ready'
           ) AS oldest_ready_pending_at
    FROM tasks AS task
    LEFT JOIN actions AS action
      ON action.task_id = task.id
     AND action.action_type = 'send_message'
     AND action.task_type = 'group_ai_chat'
    WHERE task.type = 'group_ai_chat'
      AND task.status = 'running'
      AND task.deleted_at IS NULL
    GROUP BY task.id, task.name
    ORDER BY task.name, task.id
""")

GENERATION_JOB_QUERY = text("""
    SELECT state, COUNT(*) AS job_count,
           MIN(created_at) AS oldest_created_at,
           MIN(lease_expires_at) FILTER (WHERE state = 'generating')
               AS oldest_generating_lease_expires_at
    FROM generation_jobs
    GROUP BY state
    ORDER BY state
""")


def _json_value(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def _rows(session, query) -> list[dict]:
    return [
        {key: _json_value(value) for key, value in row.items()}
        for row in session.execute(query).mappings()
    ]


def _print_rows(prefix: str, rows: list[dict]) -> None:
    print(prefix + "_SUMMARY=" + json.dumps(
        {"row_count": len(rows)}, sort_keys=True,
    ))
    for row in rows:
        print(prefix + "=" + json.dumps(row, ensure_ascii=False, sort_keys=True))


def main() -> None:
    with SessionLocal() as session:
        claims = _rows(session, AI_CLAIM_QUERY)
        activities = _rows(session, DATABASE_ACTIVITY_QUERY)
        blocking_edges = _rows(session, BLOCKING_EDGE_QUERY)
        blocking_locks = _rows(session, BLOCKING_LOCK_QUERY)
        scopes = _rows(session, RUNTIME_SCOPE_QUERY)
        task_backlogs = _rows(session, TASK_BACKLOG_QUERY)
        generation_jobs = _rows(session, GENERATION_JOB_QUERY)
    _print_rows("AI_GENERATION_GLOBAL_CLAIM", claims)
    _print_rows("AI_GENERATION_DATABASE_ACTIVITY", activities)
    _print_rows("AI_GENERATION_DATABASE_BLOCKING_EDGE", blocking_edges)
    _print_rows("AI_GENERATION_DATABASE_BLOCKING_LOCK", blocking_locks)
    _print_rows("AI_GENERATION_RUNTIME_SCOPE", scopes)
    _print_rows("AI_GENERATION_TASK_BACKLOG", task_backlogs)
    _print_rows("AI_GENERATION_JOB", generation_jobs)


if __name__ == "__main__":
    main()

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
    SELECT pid, state,
           ROUND(EXTRACT(EPOCH FROM (NOW() - query_start))::numeric, 1) AS query_age_seconds,
           wait_event_type, wait_event,
           MD5(query) AS query_fingerprint,
           CASE
             WHEN query LIKE '%ai_generation_status%'
                  AND query LIKE '%actions_2%' THEN 'ai_generation_claim'
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

RUNTIME_SCOPE_QUERY = text("""
    SELECT dispatcher_scope, contract_activation_state,
           active_contract_version, candidate_contract_version,
           runtime_shard_total, topology_fingerprint,
           capacity_config_fingerprint, updated_at
    FROM dispatch_claim_scopes
    ORDER BY updated_at DESC
    LIMIT 10
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
        scopes = _rows(session, RUNTIME_SCOPE_QUERY)
    _print_rows("AI_GENERATION_GLOBAL_CLAIM", claims)
    _print_rows("AI_GENERATION_DATABASE_ACTIVITY", activities)
    _print_rows("AI_GENERATION_RUNTIME_SCOPE", scopes)


if __name__ == "__main__":
    main()

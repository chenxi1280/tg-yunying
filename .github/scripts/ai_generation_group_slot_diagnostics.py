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


def main() -> None:
    task_ids = _task_ids()
    with SessionLocal() as session:
        rows = _rows(session, task_ids)
    role_counts: dict[str, int] = {}
    for row in rows:
        key = f"{row['group_id']}:{row['generation_role']}"
        role_counts[key] = role_counts.get(key, 0) + 1
    print("AI_GENERATION_GROUP_SLOT_SUMMARY=" + json.dumps(
        {"task_ids": task_ids, "role_counts": role_counts, "row_count": len(rows)},
        ensure_ascii=False,
        sort_keys=True,
    ))
    for row in rows:
        print("AI_GENERATION_GROUP_SLOT=" + json.dumps(
            row,
            ensure_ascii=False,
            sort_keys=True,
        ))


if __name__ == "__main__":
    main()

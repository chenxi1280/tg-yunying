from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal

from sqlalchemy import text

from app.database import SessionLocal


ACTION_CLASSIFICATION_QUERY = text("""
    WITH scoped AS (
      SELECT action.*, task.name AS task_name,
             task.task_lifecycle_epoch AS current_lifecycle_epoch,
             task.fulfillment_contract_version
      FROM actions AS action
      JOIN tasks AS task ON task.id = action.task_id
      WHERE task.type = 'group_ai_chat'
        AND task.status = 'running'
        AND task.deleted_at IS NULL
        AND task.fulfillment_contract_version = 'fact_first_v3'
        AND action.task_type = 'group_ai_chat'
        AND action.action_type = 'send_message'
        AND action.status = 'pending'
        AND action.payload ->> 'ai_generation_status' = 'ready'
        AND COALESCE(action.payload ->> 'message_text', '') <> ''
    ), classified AS (
      SELECT scoped.*,
             reservation.id AS reservation_id,
             reservation.state AS reservation_state,
             reservation.effective_claim_at AS reservation_effective_claim_at,
             reservation.source_deadline_at AS reservation_source_deadline_at,
             CASE
               WHEN scoped.task_lifecycle_epoch <> scoped.current_lifecycle_epoch
                 THEN 'lifecycle_mismatch'
               WHEN scoped.fulfillment_contract_version <> 'fact_first_v3'
                 THEN 'task_contract_mismatch'
               WHEN scoped.scheduled_at > NOW() THEN 'scheduled_future'
               WHEN scoped.pacing_slot_key IS NULL OR scoped.pacing_slot_key = ''
                 OR scoped.account_id IS NULL THEN 'candidate_without_account_pacing'
               WHEN reservation.id IS NULL THEN 'missing_open_account_reservation'
               ELSE 'direct_claim_candidate'
             END AS exclusion_reason
      FROM scoped
      LEFT JOIN LATERAL (
        SELECT pacing.id, pacing.state, pacing.effective_claim_at,
               pacing.source_deadline_at
        FROM account_pacing_reservations AS pacing
        WHERE pacing.tenant_id = scoped.tenant_id
          AND pacing.account_id = scoped.account_id
          AND pacing.pacing_slot_key = scoped.pacing_slot_key
          AND pacing.state IN ('reserved', 'bound')
        ORDER BY pacing.created_at DESC
        LIMIT 1
      ) AS reservation ON TRUE
    )
    SELECT task_id, task_name, exclusion_reason, COUNT(*) AS action_count,
           MIN(scheduled_at) AS oldest_scheduled_at,
           MAX(scheduled_at) AS latest_scheduled_at
    FROM classified
    GROUP BY task_id, task_name, exclusion_reason
    ORDER BY task_name, exclusion_reason
""")


ACTION_SAMPLE_QUERY = text("""
    WITH ranked AS (
      SELECT action.id AS action_id, action.tenant_id, action.task_id,
             task.name AS task_name,
             action.account_id, action.scheduled_at, action.release_not_before_at,
             action.effective_claim_at, action.pacing_due_at, action.pacing_slot_key,
             action.task_lifecycle_epoch, task.task_lifecycle_epoch AS current_lifecycle_epoch,
             action.result ->> 'error_code' AS last_error_code,
             ROW_NUMBER() OVER (
               PARTITION BY action.task_id ORDER BY action.scheduled_at, action.id
             ) AS task_rank
      FROM actions AS action
      JOIN tasks AS task ON task.id = action.task_id
      WHERE task.type = 'group_ai_chat'
        AND task.status = 'running'
        AND task.deleted_at IS NULL
        AND task.fulfillment_contract_version = 'fact_first_v3'
        AND action.task_type = 'group_ai_chat'
        AND action.action_type = 'send_message'
        AND action.status = 'pending'
        AND action.payload ->> 'ai_generation_status' = 'ready'
        AND COALESCE(action.payload ->> 'message_text', '') <> ''
    )
    SELECT ranked.*,
           pacing.id AS reservation_id, pacing.state AS reservation_state,
           pacing.effective_claim_at AS reservation_effective_claim_at,
           pacing.source_deadline_at AS reservation_source_deadline_at,
           admission.id AS admission_id, admission.state AS admission_state,
           admission.call_not_before_at, admission.source_gap_seconds,
           attempt.status AS attempt_status,
           attempt.gateway_call_started_at,
           source.next_call_not_before_at AS source_next_call_not_before_at,
           source.last_call_started_at AS source_last_call_started_at,
           source.last_source_gap_seconds
    FROM ranked
    LEFT JOIN account_pacing_reservations AS pacing
      ON pacing.tenant_id = ranked.tenant_id
     AND pacing.account_id = ranked.account_id
     AND pacing.pacing_slot_key = ranked.pacing_slot_key
     AND pacing.state IN ('reserved', 'bound')
    LEFT JOIN LATERAL (
      SELECT item.* FROM source_pacing_admissions AS item
      WHERE item.action_id = ranked.action_id
      ORDER BY item.updated_at DESC, item.id DESC LIMIT 1
    ) AS admission ON TRUE
    LEFT JOIN execution_attempts AS attempt ON attempt.id = admission.attempt_id
    LEFT JOIN source_pacing_states AS source ON source.id = admission.source_pacing_state_id
    WHERE ranked.task_rank <= 5
    ORDER BY ranked.task_name, ranked.task_rank
""")


ACTION_RUNTIME_REASON_QUERY = text("""
    SELECT action.task_id, task.name AS task_name,
           COALESCE(action.result ->> 'claim_released_reason', '')
             AS claim_released_reason,
           COALESCE(action.result #>> '{claim_pacing_deferred,reason_code}', '')
             AS claim_pacing_deferred_reason,
           COUNT(*) AS action_count,
           MIN(action.scheduled_at) AS oldest_scheduled_at,
           MAX(action.scheduled_at) AS latest_scheduled_at
    FROM actions AS action
    JOIN tasks AS task ON task.id = action.task_id
    WHERE task.type = 'group_ai_chat'
      AND task.status = 'running'
      AND task.deleted_at IS NULL
      AND task.fulfillment_contract_version = 'fact_first_v3'
      AND action.task_type = 'group_ai_chat'
      AND action.action_type = 'send_message'
      AND action.status = 'pending'
      AND action.payload ->> 'ai_generation_status' = 'ready'
      AND (
        action.result ->> 'claim_released_reason' <> ''
        OR action.result #>> '{claim_pacing_deferred,reason_code}' <> ''
      )
    GROUP BY action.task_id, task.name, claim_released_reason,
             claim_pacing_deferred_reason
    ORDER BY task.name, claim_released_reason, claim_pacing_deferred_reason
""")


RECENT_ATTEMPT_QUERY = text("""
    SELECT attempt.id AS attempt_id, attempt.action_id, action.task_id,
           task.name AS task_name, attempt.status, attempt.failure_type,
           attempt.before_call_at, attempt.gateway_call_started_at,
           attempt.after_call_at, action.scheduled_at,
           admission.id AS admission_id, admission.state AS admission_state,
           admission.call_not_before_at, admission.source_gap_seconds,
           source.next_call_not_before_at AS source_next_call_not_before_at,
           source.last_call_started_at AS source_last_call_started_at
    FROM execution_attempts AS attempt
    JOIN actions AS action ON action.id = attempt.action_id
    JOIN tasks AS task ON task.id = action.task_id
    LEFT JOIN source_pacing_admissions AS admission ON admission.attempt_id = attempt.id
    LEFT JOIN source_pacing_states AS source ON source.id = admission.source_pacing_state_id
    WHERE task.type = 'group_ai_chat'
      AND task.status = 'running'
      AND task.deleted_at IS NULL
      AND task.fulfillment_contract_version = 'fact_first_v3'
      AND attempt.created_at >= NOW() - INTERVAL '6 hours'
    ORDER BY attempt.created_at DESC, attempt.id DESC
    LIMIT 100
""")


SHARD_QUERY = text("""
    SELECT dispatcher_scope, shard_index, expected_capacity,
           current_worker_id, heartbeat_at, liveness_state, updated_at
    FROM dispatch_runtime_shard_states
    ORDER BY dispatcher_scope, shard_index
""")


def _json_value(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def _rows(session, query) -> list[dict]:
    result = session.execute(query).mappings()
    return [
        {key: _json_value(value) for key, value in row.items()}
        for row in result
    ]


def _print_rows(prefix: str, rows: list[dict]) -> None:
    print(prefix + "_SUMMARY=" + json.dumps({"row_count": len(rows)}, sort_keys=True))
    for row in rows:
        print(prefix + "=" + json.dumps(row, ensure_ascii=False, sort_keys=True))


def main() -> None:
    with SessionLocal() as session:
        classifications = _rows(session, ACTION_CLASSIFICATION_QUERY)
        samples = _rows(session, ACTION_SAMPLE_QUERY)
        runtime_reasons = _rows(session, ACTION_RUNTIME_REASON_QUERY)
        attempts = _rows(session, RECENT_ATTEMPT_QUERY)
        shards = _rows(session, SHARD_QUERY)
    _print_rows("AI_DISPATCH_ACTION_CLASS", classifications)
    _print_rows("AI_DISPATCH_ACTION_SAMPLE", samples)
    _print_rows("AI_DISPATCH_ACTION_RUNTIME_REASON", runtime_reasons)
    _print_rows("AI_DISPATCH_RECENT_ATTEMPT", attempts)
    _print_rows("AI_DISPATCH_SHARD", shards)


if __name__ == "__main__":
    main()

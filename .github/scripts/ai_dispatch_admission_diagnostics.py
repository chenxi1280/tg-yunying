from __future__ import annotations

import json
from datetime import date, datetime
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


CANDIDATE_SHARD_QUERY = text("""
    SELECT action.task_id, task.name AS task_name,
           MOD(action.account_id, 2) AS account_shard,
           COUNT(*) AS action_count,
           MIN(action.scheduled_at) AS oldest_scheduled_at
    FROM actions AS action
    JOIN tasks AS task ON task.id = action.task_id
    WHERE task.type = 'group_ai_chat'
      AND task.status = 'running'
      AND task.deleted_at IS NULL
      AND task.fulfillment_contract_version = 'fact_first_v3'
      AND action.action_type = 'send_message'
      AND action.status = 'pending'
      AND action.scheduled_at <= NOW()
      AND action.task_lifecycle_epoch = task.task_lifecycle_epoch
      AND action.payload ->> 'ai_generation_status' = 'ready'
      AND COALESCE(action.payload ->> 'message_text', '') <> ''
    GROUP BY action.task_id, task.name, MOD(action.account_id, 2)
    ORDER BY task.name, account_shard
""")


RECENT_WORKER_QUERY = text("""
    SELECT attempt.worker_id, MOD(attempt.account_id, 2) AS account_shard,
           action.task_id, task.name AS task_name,
           attempt.status, COUNT(*) AS attempt_count,
           MAX(attempt.created_at) AS latest_attempt_at
    FROM execution_attempts AS attempt
    JOIN actions AS action ON action.id = attempt.action_id
    JOIN tasks AS task ON task.id = action.task_id
    WHERE task.type = 'group_ai_chat'
      AND task.status = 'running'
      AND task.fulfillment_contract_version = 'fact_first_v3'
      AND attempt.created_at >= NOW() - INTERVAL '30 minutes'
    GROUP BY attempt.worker_id, MOD(attempt.account_id, 2),
             action.task_id, task.name, attempt.status
    ORDER BY attempt.worker_id, account_shard, task.name, attempt.status
""")


DEADLINE_PROJECTION_CONFLICT_QUERY = text("""
    SELECT action.id AS action_id, action.task_id, task.name AS task_name,
           task.type AS task_type, action.action_type,
           action.account_id, action.obligation_type, action.obligation_id,
           action.primary_quantity_slot_id,
           action.payload ->> 'coverage_ledger_id' AS payload_coverage_ledger_id,
           action.payload ->> 'primary_quantity_slot_id' AS payload_quantity_slot_id,
           action.payload ->> 'task_day_ledger_id' AS payload_ledger_id,
           quantity.task_day_ledger_id AS quantity_ledger_id,
           projection.id AS projection_id,
           projection.state AS projection_state,
           projection.task_day_ledger_id AS projection_ledger_id,
           projection.active_action_id,
           view_owner.task_day_ledger_id AS view_owner_ledger_id,
           payload_ledger.obligation_local_date AS payload_ledger_date,
           projection_ledger.obligation_local_date AS projection_ledger_date,
           view_ledger.obligation_local_date AS view_owner_ledger_date,
           reservation.id AS reservation_id,
           reservation.state AS reservation_state,
           reservation.source_deadline_at,
           reservation.effective_claim_at,
           action.release_not_before_at, action.scheduled_at
    FROM actions AS action
    JOIN tasks AS task ON task.id = action.task_id
    JOIN account_pacing_reservations AS reservation
      ON reservation.tenant_id = action.tenant_id
     AND reservation.account_id = action.account_id
     AND reservation.pacing_slot_key = action.pacing_slot_key
     AND reservation.state IN ('reserved', 'bound')
    LEFT JOIN task_group_daily_message_slots AS quantity
      ON quantity.id = COALESCE(
        action.primary_quantity_slot_id,
        NULLIF(action.payload ->> 'primary_quantity_slot_id', '')
      )
    LEFT JOIN fulfillment_obligation_projections AS projection
      ON projection.obligation_type = CASE
           WHEN COALESCE(action.obligation_type, '') <> ''
             AND COALESCE(action.obligation_id, '') <> ''
             THEN action.obligation_type
           WHEN COALESCE(action.payload ->> 'search_click_fulfillment_obligation_id', '') <> ''
             THEN 'search_click'
           WHEN COALESCE(action.payload ->> 'comment_fulfillment_obligation_id', '') <> ''
             THEN 'comment'
           WHEN COALESCE(action.payload ->> 'view_fulfillment_obligation_id', '') <> ''
             THEN 'view'
           WHEN COALESCE(action.payload ->> 'reaction_fulfillment_obligation_id', '') <> ''
             THEN 'reaction'
           WHEN COALESCE(action.payload ->> 'coverage_ledger_id', '') <> ''
             THEN 'coverage'
           ELSE 'quantity_slot'
         END
     AND projection.obligation_id = CASE
           WHEN COALESCE(action.obligation_type, '') <> ''
             AND COALESCE(action.obligation_id, '') <> ''
             THEN action.obligation_id
           WHEN COALESCE(action.payload ->> 'search_click_fulfillment_obligation_id', '') <> ''
             THEN action.payload ->> 'search_click_fulfillment_obligation_id'
           WHEN COALESCE(action.payload ->> 'comment_fulfillment_obligation_id', '') <> ''
             THEN action.payload ->> 'comment_fulfillment_obligation_id'
           WHEN COALESCE(action.payload ->> 'view_fulfillment_obligation_id', '') <> ''
             THEN action.payload ->> 'view_fulfillment_obligation_id'
           WHEN COALESCE(action.payload ->> 'reaction_fulfillment_obligation_id', '') <> ''
             THEN action.payload ->> 'reaction_fulfillment_obligation_id'
           WHEN COALESCE(action.payload ->> 'coverage_ledger_id', '') <> ''
             THEN action.payload ->> 'coverage_ledger_id'
           ELSE action.primary_quantity_slot_id
         END
    LEFT JOIN view_fulfillment_obligations AS view_owner
      ON view_owner.id = COALESCE(
        NULLIF(action.obligation_id, ''),
        NULLIF(action.payload ->> 'view_fulfillment_obligation_id', '')
      )
     AND action.action_type = 'view_message'
    LEFT JOIN task_day_ledgers AS payload_ledger
      ON payload_ledger.id = NULLIF(action.payload ->> 'task_day_ledger_id', '')
    LEFT JOIN task_day_ledgers AS projection_ledger
      ON projection_ledger.id = projection.task_day_ledger_id
    LEFT JOIN task_day_ledgers AS view_ledger
      ON view_ledger.id = view_owner.task_day_ledger_id
    WHERE task.status = 'running'
      AND task.fulfillment_contract_version = 'fact_first_v3'
      AND action.status = 'pending'
      AND action.task_lifecycle_epoch = task.task_lifecycle_epoch
      AND MOD(action.account_id, 2) = 1
      AND projection.task_day_ledger_id IS NOT NULL
      AND COALESCE(
        NULLIF(action.payload ->> 'task_day_ledger_id', ''),
        quantity.task_day_ledger_id
      ) IS NOT NULL
      AND projection.task_day_ledger_id <> COALESCE(
        NULLIF(action.payload ->> 'task_day_ledger_id', ''),
        quantity.task_day_ledger_id
      )
    ORDER BY reservation.source_deadline_at, action.scheduled_at, action.id
    LIMIT 30
""")


BATCH_PROJECTION_CONFLICT_QUERY = text("""
    WITH scoped AS (
      SELECT action.id AS action_id, action.task_id, task.name AS task_name,
             task.type AS task_type, action.action_type, action.account_id,
             COALESCE(
               NULLIF(action.obligation_type, ''),
               CASE
                 WHEN COALESCE(action.payload ->> 'view_fulfillment_obligation_id', '') <> '' THEN 'view'
                 WHEN COALESCE(action.payload ->> 'coverage_ledger_id', '') <> '' THEN 'coverage'
                 ELSE 'quantity_slot'
               END
             ) AS derived_obligation_type,
             COALESCE(
               NULLIF(action.obligation_id, ''),
               NULLIF(action.payload ->> 'view_fulfillment_obligation_id', ''),
               NULLIF(action.payload ->> 'coverage_ledger_id', ''),
               action.primary_quantity_slot_id
             ) AS derived_obligation_id,
             COALESCE(
               NULLIF(action.payload ->> 'task_day_ledger_id', ''),
               quantity.task_day_ledger_id
             ) AS owner_ledger_id,
             action.scheduled_at
      FROM actions AS action
      JOIN tasks AS task ON task.id = action.task_id
      LEFT JOIN task_group_daily_message_slots AS quantity
        ON quantity.id = COALESCE(
          action.primary_quantity_slot_id,
          NULLIF(action.payload ->> 'primary_quantity_slot_id', '')
        )
      WHERE task.status = 'running'
        AND task.fulfillment_contract_version = 'fact_first_v3'
        AND action.status = 'pending'
        AND action.task_lifecycle_epoch = task.task_lifecycle_epoch
        AND MOD(action.account_id, 2) = 1
    ), conflicting AS (
      SELECT derived_obligation_type, derived_obligation_id
      FROM scoped
      WHERE derived_obligation_id IS NOT NULL AND owner_ledger_id IS NOT NULL
      GROUP BY derived_obligation_type, derived_obligation_id
      HAVING COUNT(DISTINCT owner_ledger_id) > 1
    )
    SELECT scoped.*
    FROM scoped
    JOIN conflicting USING (derived_obligation_type, derived_obligation_id)
    ORDER BY scoped.derived_obligation_type, scoped.derived_obligation_id,
             scoped.scheduled_at, scoped.action_id
    LIMIT 50
""")


def _json_value(value):
    if isinstance(value, (date, datetime)):
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


GROUP_QUERY = text("""
    SELECT DISTINCT g.id, g.title, g.group_type, g.auth_status, g.can_send,
           g.listener_enabled, g.listener_cursor_status,
           g.listener_last_polled_at, g.listener_last_error,
           g.listener_interval_seconds,
           t.name AS task_name, t.id AS task_id
    FROM tg_groups AS g
    JOIN tasks AS t ON t.deleted_at IS NULL AND t.type = 'group_ai_chat'
      AND (t.type_config ->> 'target_group_id')::text = g.id::text
    ORDER BY t.name
""")


AI_PROVIDERS_QUERY = text("""
    SELECT id, provider_name, model_name, is_active, credential_enabled, health_status, last_error
    FROM ai_providers
    ORDER BY id ASC
""")

AI_SETTINGS_QUERY = text("""
    SELECT tenant_id, default_provider_id, ai_enabled,
           ai_group_model_fallback_enabled, ai_provider_route_fallback_enabled
    FROM tenant_ai_settings
""")

AI_ROUTES_QUERY = text("""
    SELECT rs.id AS route_set_id, rs.purpose AS route_set_purpose,
           rs.revision AS route_set_revision, rs.status AS route_set_status,
           ri.priority, ri.provider_id, ri.model_name, ri.enabled,
           p.provider_name, p.health_status
    FROM tenant_ai_provider_route_sets AS rs
    JOIN tenant_ai_provider_route_items AS ri ON ri.route_set_id = rs.id
    JOIN ai_providers AS p ON p.id = ri.provider_id
    ORDER BY rs.id, ri.priority ASC
""")


GENERATION_CANDIDATES_QUERY = text("""
    SELECT t.id AS task_id, t.name AS task_name,
           t.status AS task_status,
           t.fulfillment_contract_version,
           t.task_lifecycle_epoch AS current_task_epoch,
           COUNT(a.id) AS total_pending_actions,
           MIN(a.scheduled_at) AS min_scheduled_at,
           MAX(a.scheduled_at) AS max_scheduled_at,
           COUNT(CASE WHEN a.scheduled_at <= NOW() THEN 1 END) AS sched_past,
           COUNT(CASE WHEN a.scheduled_at <= NOW() + INTERVAL '30 minutes' THEN 1 END) AS sched_due_in_30m,
           COUNT(CASE WHEN a.task_lifecycle_epoch = t.task_lifecycle_epoch THEN 1 END) AS epoch_match_count,
           COUNT(CASE WHEN a.task_lifecycle_epoch <> t.task_lifecycle_epoch THEN 1 END) AS epoch_mismatch_count,
           COUNT(CASE WHEN a.account_id IS NOT NULL THEN 1 END) AS has_account_count,
           COUNT(CASE WHEN a.account_id IS NULL THEN 1 END) AS no_account_count,
           COUNT(CASE WHEN a.payload ->> 'ai_generation_status' = 'pending' THEN 1 END) AS gen_pending_count,
           COUNT(CASE WHEN a.payload ->> 'ai_generation_status' = 'ready' THEN 1 END) AS gen_ready_count,
           COUNT(CASE WHEN a.payload ->> 'ai_generation_status' NOT IN ('pending', 'ready') THEN 1 END) AS gen_other_count
    FROM actions AS a
    JOIN tasks AS t ON t.id = a.task_id
    WHERE a.task_type = 'group_ai_chat'
      AND a.action_type = 'send_message'
      AND a.status = 'pending'
    GROUP BY t.id, t.name, t.status, t.fulfillment_contract_version, t.task_lifecycle_epoch
    ORDER BY t.name
""")

GENERATION_JOBS_QUERY = text("""
    SELECT state, COUNT(*) AS job_count,
           MIN(generation_not_before_at) AS min_not_before,
           MAX(generation_not_before_at) AS max_not_before,
           COUNT(CASE WHEN generation_not_before_at > NOW() THEN 1 END) AS future_not_before_count,
           COUNT(CASE WHEN next_retry_at > NOW() THEN 1 END) AS future_retry_count
    FROM generation_jobs
    GROUP BY state
    ORDER BY state
""")

WORKER_HEARTBEATS_QUERY = text("""
    SELECT worker_id, process_type, hostname, pid, status, last_seen_at
    FROM worker_heartbeats
    ORDER BY last_seen_at DESC
""")

RUNNING_TASKS_DETAILED_QUERY = text("""
    SELECT t.id, t.name, t.status, t.type, t.task_lifecycle_epoch, t.fulfillment_contract_version,
           t.type_config->>'target_group_id' AS target_group_id,
           t.type_config->>'daily_message_target' AS daily_message_target,
           g.id AS actual_group_id, g.title AS group_title, g.can_send, g.auth_status,
           tgt.effective_message_target, tgt.configured_message_target,
           (SELECT COUNT(*) FROM actions WHERE task_id = t.id AND status = 'pending') AS pending_action_count,
           (SELECT COUNT(*) FROM actions WHERE task_id = t.id AND status = 'confirmed' AND scheduled_at >= CURRENT_DATE) AS today_confirmed_count,
           (SELECT COUNT(*) FROM actions WHERE task_id = t.id AND status = 'failed' AND scheduled_at >= CURRENT_DATE) AS today_failed_count,
           (SELECT COUNT(*) FROM actions WHERE task_id = t.id AND payload->>'ai_generation_status' = 'ready' AND status = 'pending') AS ready_pending_count,
           t.updated_at, t.last_error
    FROM tasks AS t
    LEFT JOIN tg_groups AS g ON g.id = (t.type_config->>'target_group_id')::bigint
    LEFT JOIN task_group_daily_targets AS tgt ON tgt.task_id = t.id AND tgt.target_date = CURRENT_DATE
    WHERE t.status = 'running'
    ORDER BY t.name
""")

ZHENGDA_ACTIONS_BREAKDOWN_QUERY = text("""
    SELECT a.task_id, a.status, a.action_type,
           a.payload->>'ai_generation_status' AS gen_status,
           COUNT(*) AS count,
           MIN(a.scheduled_at) AS min_scheduled_at,
           MAX(a.scheduled_at) AS max_scheduled_at,
           MIN(a.created_at) AS min_created_at,
           MAX(a.created_at) AS max_created_at,
           COUNT(CASE WHEN a.payload->>'message_text' IS NOT NULL AND a.payload->>'message_text' <> '' THEN 1 END) AS has_text_count
    FROM actions AS a
    WHERE a.task_id = 'a52e84f2-8663-4b00-bbbe-196fb626b28d'
    GROUP BY a.task_id, a.status, a.action_type, a.payload->>'ai_generation_status'
    ORDER BY a.task_id, a.status, a.action_type
""")


RECENT_HOURLY_SEND_STATS_QUERY = text("""
    SELECT t.id AS task_id, t.name AS task_name,
           COUNT(CASE WHEN a.status IN ('success', 'confirmed') AND a.action_type = 'send_message' AND a.executed_at >= NOW() - INTERVAL '1 hour' THEN 1 END) AS sent_last_1h,
           COUNT(CASE WHEN a.status IN ('success', 'confirmed') AND a.action_type = 'send_message' AND a.executed_at >= CURRENT_DATE THEN 1 END) AS sent_today,
           MAX(CASE WHEN a.status IN ('success', 'confirmed') AND a.action_type = 'send_message' THEN a.executed_at END) AS latest_sent_at,
           COUNT(CASE WHEN a.status = 'pending' AND a.action_type = 'send_message' AND a.payload->>'ai_generation_status' = 'ready' THEN 1 END) AS ready_to_send,
           COUNT(CASE WHEN a.status = 'pending' AND a.action_type = 'send_message' AND a.payload->>'ai_generation_status' = 'pending' THEN 1 END) AS pending_generation,
           MIN(CASE WHEN a.status = 'pending' AND a.action_type = 'send_message' THEN a.scheduled_at END) AS next_scheduled_at,
           MAX(CASE WHEN a.status = 'pending' AND a.action_type = 'send_message' THEN a.scheduled_at END) AS max_scheduled_at
    FROM tasks AS t
    LEFT JOIN actions AS a ON a.task_id = t.id
    WHERE t.status = 'running' AND t.type = 'group_ai_chat'
    GROUP BY t.id, t.name
    ORDER BY t.name
""")

TODAY_SUCCESS_QUERY = text("""
    SELECT a.id, a.task_id, t.name AS task_name, a.account_id, a.action_type, a.status,
           a.scheduled_at, a.created_at, a.executed_at,
           a.payload->>'ai_generation_status' AS gen_status,
           a.payload->>'message_text' AS message_text,
           a.result
    FROM actions AS a
    JOIN tasks AS t ON t.id = a.task_id
    WHERE t.status = 'running'
      AND a.status IN ('success', 'confirmed')
      AND (a.scheduled_at >= CURRENT_DATE OR a.executed_at >= CURRENT_DATE)
    ORDER BY a.executed_at DESC NULLS LAST, a.created_at DESC
    LIMIT 30
""")


ACCOUNT_OVERALL_SUMMARY_QUERY = text("""
    SELECT
        COUNT(*) AS total_accounts,
        COUNT(CASE WHEN status = 'active' THEN 1 END) AS active_accounts,
        COUNT(CASE WHEN status = 'banned' THEN 1 END) AS banned_accounts,
        COUNT(CASE WHEN status = 'pending_login' THEN 1 END) AS pending_login_accounts,
        COUNT(CASE WHEN status NOT IN ('active', 'banned', 'pending_login') THEN 1 END) AS other_status_accounts,
        COUNT(CASE WHEN health_score >= 80 THEN 1 END) AS high_health_accounts
    FROM tg_accounts
    WHERE deleted_at IS NULL
""")

ACCOUNT_TODAY_SEND_COVERAGE_QUERY = text("""
    SELECT
        COUNT(DISTINCT a.account_id) AS distinct_senders_today,
        COUNT(DISTINCT CASE WHEN a.executed_at >= NOW() - INTERVAL '1 hour' THEN a.account_id END) AS distinct_senders_last_1h,
        COUNT(DISTINCT a.task_id) AS distinct_active_tasks,
        COUNT(*) AS total_success_messages,
        MIN(a.count_per_account) AS min_sends_per_sender,
        ROUND(AVG(a.count_per_account), 2) AS avg_sends_per_sender,
        MAX(a.count_per_account) AS max_sends_per_sender
    FROM (
        SELECT account_id, task_id, executed_at,
               COUNT(*) OVER(PARTITION BY account_id) AS count_per_account
        FROM actions
        WHERE status IN ('success', 'confirmed')
          AND action_type = 'send_message'
          AND (scheduled_at >= CURRENT_DATE OR executed_at >= CURRENT_DATE)
    ) AS a
""")

ACCOUNT_TASK_COVERAGE_BREAKDOWN_QUERY = text("""
    SELECT t.name AS task_name,
           COUNT(DISTINCT CASE WHEN a.status IN ('success', 'confirmed') THEN a.account_id END) AS distinct_senders_today,
           COUNT(DISTINCT CASE WHEN a.status = 'pending' THEN a.account_id END) AS distinct_planned_accounts_pending,
           COUNT(DISTINCT a.account_id) AS total_assigned_distinct_accounts,
           COUNT(CASE WHEN a.status IN ('success', 'confirmed') THEN 1 END) AS total_sent_messages
    FROM tasks AS t
    LEFT JOIN actions AS a ON a.task_id = t.id AND (a.scheduled_at >= CURRENT_DATE OR a.executed_at >= CURRENT_DATE) AND a.action_type = 'send_message'
    WHERE t.status = 'running' AND t.type = 'group_ai_chat'
    GROUP BY t.id, t.name
    ORDER BY t.name
""")

ACCOUNT_SEND_FREQUENCY_DISTRIBUTION_QUERY = text("""
    SELECT sends_bucket, COUNT(*) AS account_count
    FROM (
        SELECT account_id,
               CASE
                   WHEN COUNT(*) = 1 THEN '1_message'
                   WHEN COUNT(*) = 2 THEN '2_messages'
                   WHEN COUNT(*) = 3 THEN '3_messages'
                   WHEN COUNT(*) BETWEEN 4 AND 5 THEN '4_5_messages'
                   ELSE '6+_messages'
               END AS sends_bucket
        FROM actions
        WHERE status IN ('success', 'confirmed')
          AND action_type = 'send_message'
          AND (scheduled_at >= CURRENT_DATE OR executed_at >= CURRENT_DATE)
        GROUP BY account_id
    ) AS sub
    GROUP BY sends_bucket
    ORDER BY sends_bucket
""")


ACCOUNT_ATTRIBUTES_BREAKDOWN_QUERY = text("""
    SELECT
        status,
        account_identity,
        (pool_id IS NOT NULL) AS has_pool,
        (session_ciphertext IS NOT NULL AND session_ciphertext <> '') AS has_session,
        (deleted_at IS NULL) AS is_not_deleted,
        COUNT(*) AS account_count
    FROM tg_accounts
    GROUP BY status, account_identity, (pool_id IS NOT NULL), (session_ciphertext IS NOT NULL AND session_ciphertext <> ''), (deleted_at IS NULL)
    ORDER BY account_count DESC
""")

TASK_MEMBERSHIP_AND_COVERAGE_QUERY = text("""
    SELECT 
        t.name AS task_name,
        t.id AS task_id,
        (SELECT COUNT(*) FROM task_membership_admission_items WHERE task_id = t.id) AS membership_items_count,
        (SELECT COUNT(*) FROM task_account_daily_coverage WHERE task_id = t.id AND targeted_at = CURRENT_DATE) AS daily_coverage_items_count,
        (SELECT COUNT(*) FROM tg_group_memberships WHERE group_id = (t.type_config->>'target_group_id')::bigint AND is_member = TRUE) AS actual_group_members_count
    FROM tasks AS t
    WHERE t.status = 'running' AND t.type = 'group_ai_chat'
    ORDER BY t.name
""")


def main() -> None:
    with SessionLocal() as session:
        classifications = _rows(session, ACTION_CLASSIFICATION_QUERY)
        samples = _rows(session, ACTION_SAMPLE_QUERY)
        runtime_reasons = _rows(session, ACTION_RUNTIME_REASON_QUERY)
        attempts = _rows(session, RECENT_ATTEMPT_QUERY)
        shards = _rows(session, SHARD_QUERY)
        candidate_shards = _rows(session, CANDIDATE_SHARD_QUERY)
        recent_workers = _rows(session, RECENT_WORKER_QUERY)
        deadline_conflicts = _rows(session, DEADLINE_PROJECTION_CONFLICT_QUERY)
        batch_conflicts = _rows(session, BATCH_PROJECTION_CONFLICT_QUERY)
        group_states = _rows(session, GROUP_QUERY)
        ai_providers = _rows(session, AI_PROVIDERS_QUERY)
        ai_settings = _rows(session, AI_SETTINGS_QUERY)
        ai_routes = _rows(session, AI_ROUTES_QUERY)
        gen_candidates = _rows(session, GENERATION_CANDIDATES_QUERY)
        gen_jobs = _rows(session, GENERATION_JOBS_QUERY)
        worker_hbs = _rows(session, WORKER_HEARTBEATS_QUERY)
        running_tasks = _rows(session, RUNNING_TASKS_DETAILED_QUERY)
        zhengda_actions = _rows(session, ZHENGDA_ACTIONS_BREAKDOWN_QUERY)
        today_success = _rows(session, TODAY_SUCCESS_QUERY)
        recent_stats = _rows(session, RECENT_HOURLY_SEND_STATS_QUERY)
        account_overall = _rows(session, ACCOUNT_OVERALL_SUMMARY_QUERY)
        account_today_coverage = _rows(session, ACCOUNT_TODAY_SEND_COVERAGE_QUERY)
        account_task_coverage = _rows(session, ACCOUNT_TASK_COVERAGE_BREAKDOWN_QUERY)
        account_freq_dist = _rows(session, ACCOUNT_SEND_FREQUENCY_DISTRIBUTION_QUERY)
        account_attrs = _rows(session, ACCOUNT_ATTRIBUTES_BREAKDOWN_QUERY)
        task_scope_items = _rows(session, TASK_MEMBERSHIP_AND_COVERAGE_QUERY)
    _print_rows("AI_DISPATCH_ACTION_CLASS", classifications)
    _print_rows("AI_DISPATCH_ACTION_SAMPLE", samples)
    _print_rows("AI_DISPATCH_ACTION_RUNTIME_REASON", runtime_reasons)
    _print_rows("AI_DISPATCH_RECENT_ATTEMPT", attempts)
    _print_rows("AI_DISPATCH_SHARD", shards)
    _print_rows("AI_DISPATCH_CANDIDATE_SHARD", candidate_shards)
    _print_rows("AI_DISPATCH_RECENT_WORKER", recent_workers)
    _print_rows("AI_DISPATCH_DEADLINE_PROJECTION", deadline_conflicts)
    _print_rows("AI_DISPATCH_BATCH_PROJECTION", batch_conflicts)
    _print_rows("AI_DISPATCH_GROUP_STATE", group_states)
    _print_rows("AI_DISPATCH_AI_PROVIDERS", ai_providers)
    _print_rows("AI_DISPATCH_AI_SETTINGS", ai_settings)
    _print_rows("AI_DISPATCH_AI_ROUTES", ai_routes)
    _print_rows("AI_GENERATION_PIPELINE_CANDIDATES", gen_candidates)
    _print_rows("AI_GENERATION_PIPELINE_JOBS", gen_jobs)
    _print_rows("AI_WORKER_HEARTBEATS", worker_hbs)
    _print_rows("RUNNING_TASKS_DETAILED", running_tasks)
    _print_rows("ZHENGDA_ACTIONS_BREAKDOWN", zhengda_actions)
    _print_rows("ZHENGDA_TODAY_SUCCESS_OR_PENDING", today_success)
    _print_rows("RECENT_HOURLY_SEND_STATS", recent_stats)
    _print_rows("ACCOUNT_OVERALL_SUMMARY", account_overall)
    _print_rows("ACCOUNT_TODAY_SEND_COVERAGE", account_today_coverage)
    _print_rows("ACCOUNT_TASK_COVERAGE_BREAKDOWN", account_task_coverage)
    _print_rows("ACCOUNT_SEND_FREQUENCY_DISTRIBUTION", account_freq_dist)
    _print_rows("ACCOUNT_ATTRIBUTES_BREAKDOWN", account_attrs)
    _print_rows("TASK_MEMBERSHIP_AND_COVERAGE", task_scope_items)


if __name__ == "__main__":
    main()


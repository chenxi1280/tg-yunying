from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import bindparam, text

from app.database import SessionLocal


DEFAULT_TASK_NAMES = (
    "郑州大学",
    "郑州师范",
    "郑州楼凤",
    "郑州学生会",
)
LOCAL_TIMEZONE = ZoneInfo("Asia/Shanghai")
RECENT_TASK_LOOKBACK = timedelta(days=7)


def _rows(session, statement: str, **params) -> list[dict]:
    return [
        dict(row)
        for row in session.execute(text(statement), params).mappings()
    ]


def _tasks(
    session,
    names: tuple[str, ...],
    *,
    recent_since: datetime,
) -> list[dict]:
    statement = text(
        """
        SELECT id, tenant_id, name, type, status, timezone, next_run_at,
               hard_hourly_next_check_at, last_error, created_at, updated_at
        FROM tasks
        WHERE deleted_at IS NULL
          AND type = 'group_ai_chat'
          AND (
              name IN :names
              OR created_at >= :recent_since
          )
        ORDER BY created_at DESC, name
        """
    ).bindparams(bindparam("names", expanding=True))
    return [
        dict(row)
        for row in session.execute(
            statement,
            {"names": names, "recent_since": recent_since},
        ).mappings()
    ]


def _ledger_rows(session, task_id: str, local_date) -> list[dict]:
    return _rows(
        session,
        """
        SELECT id, obligation_local_date, period_start_at, deadline_at,
               day_phase, lifecycle_status
        FROM task_day_ledgers
        WHERE task_id = :task_id AND obligation_local_date = :local_date
        ORDER BY period_start_at DESC
        """,
        task_id=task_id,
        local_date=local_date,
    )


def _target_rows(session, task_id: str, local_date) -> list[dict]:
    return _rows(
        session,
        """
        SELECT id, group_id, configured_message_target, frozen_account_count,
               effective_message_target, due_message_count,
               confirmed_message_count, coverage_confirmed_account_count,
               daily_fulfillment_phase
        FROM task_group_daily_targets
        WHERE task_id = :task_id AND target_date = :local_date
        """,
        task_id=task_id,
        local_date=local_date,
    )


def _coverage_rows(session, task_id: str, local_date) -> list[dict]:
    return _rows(
        session,
        """
        SELECT state, blocker_code, COUNT(*) AS row_count,
               SUM(target_count) AS target_count,
               SUM(confirmed_count) AS confirmed_count,
               COUNT(*) FILTER (
                   WHERE confirmed_count < target_count
               ) AS incomplete_count
        FROM task_account_daily_coverage
        WHERE task_id = :task_id AND coverage_date = :local_date
        GROUP BY state, blocker_code
        ORDER BY state, blocker_code
        """,
        task_id=task_id,
        local_date=local_date,
    )


def _quantity_rows(session, task_id: str, ledger_id: str) -> list[dict]:
    return _rows(
        session,
        """
        SELECT s.slot_kind, s.state,
               COALESCE(c.state, 'unbound') AS coverage_state,
               CASE
                   WHEN c.id IS NULL THEN 'unbound'
                   WHEN c.confirmed_count >= c.target_count THEN 'confirmed'
                   ELSE 'incomplete'
               END AS coverage_progress,
               COUNT(*) AS slot_count
        FROM task_group_daily_message_slots s
        LEFT JOIN task_account_daily_coverage c
          ON c.id = s.task_account_daily_coverage_id
        WHERE s.task_id = :task_id AND s.task_day_ledger_id = :ledger_id
        GROUP BY s.slot_kind, s.state, coverage_state, coverage_progress
        ORDER BY s.slot_kind, s.state, coverage_state, coverage_progress
        """,
        task_id=task_id,
        ledger_id=ledger_id,
    )


def _action_rows(session, task_id: str, period_start_at) -> list[dict]:
    return _rows(
        session,
        """
        SELECT a.status,
               COALESCE(a.payload ->> 'ai_generation_status', '') AS generation_status,
               COALESCE(a.result ->> 'error_code', '') AS error_code,
               COUNT(DISTINCT a.id) AS action_count,
               COUNT(DISTINCT a.id) FILTER (
                   WHERE COALESCE(a.result ->> 'remote_message_id', '') <> ''
               ) AS action_remote_count,
               COUNT(DISTINCT e.id) FILTER (
                   WHERE COALESCE(e.remote_message_id, '') <> ''
               ) AS attempt_remote_count
        FROM actions a
        LEFT JOIN execution_attempts e ON e.action_id = a.id
        WHERE a.task_id = :task_id
          AND a.action_type = 'send_message'
          AND a.created_at >= :period_start_at
        GROUP BY a.status, generation_status, error_code
        ORDER BY a.status, generation_status, error_code
        """,
        task_id=task_id,
        period_start_at=period_start_at,
    )


def _unknown_action_rows(session, task_id: str, period_start_at) -> list[dict]:
    return _rows(
        session,
        """
        SELECT a.id AS action_id, a.account_id, a.created_at, a.executed_at,
               e.id AS attempt_id, e.status AS attempt_status,
               e.remote_message_id AS attempt_remote_message_id,
               r.id AS reconcile_case_id, r.state AS reconcile_case_state,
               j.state AS journal_state,
               encode(decode(j.evidence_hash, 'hex'), 'base64') AS journal_evidence_fingerprint_b64,
               j.remote_mutation_state,
               j.remote_message_id AS journal_remote_message_id,
               c.id AS coverage_id, c.state AS coverage_state,
               c.blocker_code AS coverage_blocker_code,
               s.id AS quantity_slot_id, s.state AS quantity_slot_state,
               cs.id AS content_mix_slot_id, cs.slot_state
        FROM actions a
        LEFT JOIN LATERAL (
            SELECT candidate.* FROM execution_attempts candidate
            WHERE candidate.action_id = a.id
            ORDER BY candidate.attempt_no DESC LIMIT 1
        ) e ON TRUE
        LEFT JOIN remote_reconcile_cases r
          ON r.action_id = a.id AND r.execution_attempt_id = e.id
        LEFT JOIN gateway_request_evidence_journals j
          ON j.action_id = a.id AND j.execution_attempt_id = e.id
        LEFT JOIN task_account_daily_coverage c
          ON c.id = a.payload ->> 'coverage_ledger_id'
        LEFT JOIN task_group_daily_message_slots s
          ON s.id = a.primary_quantity_slot_id
        LEFT JOIN content_mix_cycle_slots cs
          ON cs.id = a.content_mix_cycle_slot_id
        WHERE a.task_id = :task_id
          AND a.action_type = 'send_message'
          AND a.status = 'unknown_after_send'
          AND a.created_at >= :period_start_at
        ORDER BY a.created_at DESC LIMIT 8
        """,
        task_id=task_id,
        period_start_at=period_start_at,
    )


def _admission_action_rows(session, task_id: str) -> list[dict]:
    return _rows(
        session,
        """
        SELECT action_type, status,
               COALESCE(result ->> 'error_code', '') AS error_code,
               COUNT(*) AS action_count
        FROM actions
        WHERE task_id = :task_id
          AND action_type IN (
              'ensure_target_membership',
              'ensure_channel_membership',
              'group_bot_confirmation_button'
          )
        GROUP BY action_type, status, error_code
        ORDER BY action_type, status, error_code
        """,
        task_id=task_id,
    )


def _binding_rows(session, task_id: str, ledger_id: str) -> list[dict]:
    return _rows(
        session,
        """
        SELECT COUNT(*) AS checked_actions,
               COUNT(*) FILTER (
                   WHERE c.id IS NULL
                      OR a.account_id IS DISTINCT FROM c.account_id
               ) AS account_binding_mismatches,
               COUNT(*) FILTER (
                   WHERE cs.id IS NULL
                      OR cs.primary_quantity_slot_id IS DISTINCT
                         FROM a.primary_quantity_slot_id
               ) AS content_mix_binding_mismatches
        FROM actions a
        LEFT JOIN task_group_daily_message_slots s
          ON s.id = a.primary_quantity_slot_id
        LEFT JOIN task_account_daily_coverage c
          ON c.id = s.task_account_daily_coverage_id
        LEFT JOIN content_mix_cycle_slots cs
          ON cs.id = a.content_mix_cycle_slot_id
        WHERE a.task_id = :task_id
          AND s.task_day_ledger_id = :ledger_id
          AND a.action_type = 'send_message'
          AND a.content_mix_cycle_slot_id IS NOT NULL
        """,
        task_id=task_id,
        ledger_id=ledger_id,
    )


def _content_mix_rows(session, task_id: str, ledger_id: str) -> list[dict]:
    return _rows(
        session,
        """
        SELECT c.materialization_status, c.settlement_status,
               s.slot_state, s.terminal_reason, COUNT(*) AS slot_count
        FROM content_mix_cycles c
        JOIN content_mix_cycle_slots s ON s.cycle_id = c.id
        WHERE c.task_id = :task_id AND c.task_day_ledger_id = :ledger_id
        GROUP BY c.materialization_status, c.settlement_status,
                 s.slot_state, s.terminal_reason
        ORDER BY c.materialization_status, c.settlement_status,
                 s.slot_state, s.terminal_reason
        """,
        task_id=task_id,
        ledger_id=ledger_id,
    )


def _admission_rows(session, task_id: str, local_date) -> list[dict]:
    return _rows(
        session,
        """
        SELECT COALESCE(a.state, 'missing') AS admission_state,
               COALESCE(a.terminal_reason, '') AS terminal_reason,
               COALESCE(a.observation_gap, false) AS observation_gap,
               COUNT(*) AS coverage_count,
               COUNT(*) FILTER (
                   WHERE c.confirmed_count < c.target_count
               ) AS incomplete_count,
               COUNT(*) FILTER (
                   WHERE a.state = 'observing'
                     AND a.observation_gap = false
                     AND a.no_prompt_pass_at <= now()
               ) AS observation_due_count
        FROM task_account_daily_coverage c
        LEFT JOIN task_group_bot_admissions a
          ON a.task_id = c.task_id
         AND a.target_group_id = c.group_id
         AND a.account_id = c.account_id
        WHERE c.task_id = :task_id AND c.coverage_date = :local_date
        GROUP BY admission_state, terminal_reason, observation_gap
        ORDER BY admission_state, terminal_reason, observation_gap
        """,
        task_id=task_id,
        local_date=local_date,
    )


def _admission_protocol_rows(session, group_id: int) -> list[dict]:
    return _rows(
        session,
        """
        SELECT state, completion_policy, trusted_bot_peer_id, failure_code,
               COUNT(*) AS admission_count
        FROM group_bot_admissions
        WHERE group_id = :group_id
        GROUP BY state, completion_policy, trusted_bot_peer_id, failure_code
        ORDER BY state, completion_policy, trusted_bot_peer_id, failure_code
        """,
        group_id=group_id,
    )


def _admission_policy_rows(session, group_id: int) -> list[dict]:
    return _rows(
        session,
        """
        SELECT id, completion_policy, trusted_bot_peer_id, evidence_ref,
               reason, policy_version, status, created_by, effective_at
        FROM group_bot_admission_policies
        WHERE group_id = :group_id
        ORDER BY policy_version DESC, id DESC
        """,
        group_id=group_id,
    )


def _group_bot_context_rows(session, group_id: int) -> list[dict]:
    return _rows(
        session,
        """
        SELECT COUNT(*) AS context_count,
               COUNT(*) FILTER (WHERE is_bot) AS bot_message_count,
               COUNT(*) FILTER (
                   WHERE json_array_length(control_buttons) > 0
               ) AS button_message_count,
               MAX(id) AS latest_context_id,
               MAX(COALESCE(sent_at, created_at)) AS latest_context_at
        FROM group_context_messages
        WHERE group_id = :group_id
          AND COALESCE(sent_at, created_at) >= NOW() - INTERVAL '24 hours'
        """,
        group_id=group_id,
    )


def _group_bot_sender_rows(session, group_id: int) -> list[dict]:
    return _rows(
        session,
        """
        SELECT sender_peer_id, sender_name, sender_role,
               COUNT(*) AS message_count,
               COUNT(*) FILTER (
                   WHERE json_array_length(control_buttons) > 0
               ) AS button_message_count,
               MAX(COALESCE(sent_at, created_at)) AS latest_message_at
        FROM group_context_messages
        WHERE group_id = :group_id
          AND is_bot
          AND COALESCE(sent_at, created_at) >= NOW() - INTERVAL '24 hours'
        GROUP BY sender_peer_id, sender_name, sender_role
        ORDER BY message_count DESC
        LIMIT 20
        """,
        group_id=group_id,
    )


def _group_bot_sample_rows(session, group_id: int) -> list[dict]:
    return _rows(
        session,
        """
        SELECT remote_message_id, sender_peer_id, sender_name,
               LEFT(content, 500) AS content,
               control_buttons, COALESCE(sent_at, created_at) AS message_at
        FROM group_context_messages
        WHERE group_id = :group_id
          AND is_bot
          AND json_array_length(control_buttons) > 0
        ORDER BY COALESCE(sent_at, created_at) DESC, id DESC
        LIMIT 8
        """,
        group_id=group_id,
    )


def _prior_residue(session, task_id: str, local_date) -> list[dict]:
    return _rows(
        session,
        """
        SELECT l.obligation_local_date, c.id AS cycle_id,
               c.materialization_status, c.settlement_status,
               COUNT(DISTINCT s.id) AS slot_count,
               COUNT(DISTINCT a.id) AS action_count,
               COUNT(DISTINCT e.id) FILTER (
                   WHERE COALESCE(e.remote_message_id, '') <> ''
               ) AS remote_attempt_count
        FROM content_mix_cycles c
        JOIN task_day_ledgers l ON l.id = c.task_day_ledger_id
        LEFT JOIN content_mix_cycle_slots s ON s.cycle_id = c.id
        LEFT JOIN actions a ON a.content_mix_cycle_slot_id = s.id
        LEFT JOIN execution_attempts e ON e.action_id = a.id
        WHERE c.task_id = :task_id
          AND l.obligation_local_date < :local_date
          AND (
              c.settlement_status <> 'settled'
              OR s.slot_state IN ('unmaterialized', 'replan_pending')
          )
        GROUP BY l.obligation_local_date, c.id,
                 c.materialization_status, c.settlement_status
        ORDER BY l.obligation_local_date, c.id
        """,
        task_id=task_id,
        local_date=local_date,
    )


def _task_snapshot(session, task: dict, local_date) -> dict:
    ledgers = _ledger_rows(session, task["id"], local_date)
    ledger = ledgers[0] if ledgers else None
    targets = _target_rows(session, task["id"], local_date)
    snapshot = {
        "task": task,
        "target": targets,
        "coverage": _coverage_rows(session, task["id"], local_date),
        "admissions": _admission_rows(session, task["id"], local_date),
        "admission_actions": _admission_action_rows(session, task["id"]),
        "prior_residue": _prior_residue(session, task["id"], local_date),
        "ledger": ledger,
    }
    if targets:
        group_id = int(targets[0]["group_id"])
        snapshot["admission_protocols"] = _admission_protocol_rows(
            session, group_id,
        )
        snapshot["admission_policies"] = _admission_policy_rows(
            session, group_id,
        )
        snapshot["group_bot_context"] = _group_bot_context_rows(
            session, group_id,
        )
        snapshot["group_bot_senders"] = _group_bot_sender_rows(
            session, group_id,
        )
        snapshot["group_bot_samples"] = _group_bot_sample_rows(
            session, group_id,
        )
    if ledger:
        snapshot["quantity_slots"] = _quantity_rows(
            session, task["id"], ledger["id"]
        )
        snapshot["actions"] = _action_rows(
            session, task["id"], ledger["period_start_at"]
        )
        snapshot["unknown_actions"] = _unknown_action_rows(
            session, task["id"], ledger["period_start_at"]
        )
        snapshot["bindings"] = _binding_rows(
            session, task["id"], ledger["id"]
        )
        snapshot["content_mix"] = _content_mix_rows(
            session, task["id"], ledger["id"]
        )
    return snapshot


def diagnose(names: tuple[str, ...]) -> dict:
    captured_at = datetime.now(LOCAL_TIMEZONE)
    with SessionLocal() as session:
        tasks = _tasks(
            session,
            names,
            recent_since=captured_at - RECENT_TASK_LOOKBACK,
        )
        return {
            "captured_at": captured_at.isoformat(timespec="seconds"),
            "local_date": captured_at.date(),
            "dispatch_scope": _rows(
                session,
                """
                SELECT dispatcher_scope, claim_capacity, active_claim_count,
                       updated_at
                FROM dispatch_claim_scopes
                ORDER BY updated_at DESC
                """,
            ),
            "tasks": [
                _task_snapshot(session, task, captured_at.date())
                for task in tasks
            ],
        }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only diagnosis for blocked AI group tasks."
    )
    parser.add_argument("--task-name", action="append", dest="task_names")
    args = parser.parse_args()
    names = tuple(args.task_names or DEFAULT_TASK_NAMES)
    print(json.dumps(diagnose(names), ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

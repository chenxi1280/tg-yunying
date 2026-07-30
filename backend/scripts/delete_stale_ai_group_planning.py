from __future__ import annotations

import argparse
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import bindparam, text

from app.database import SessionLocal


LOCAL_TIMEZONE = ZoneInfo("Asia/Shanghai")


def _target_cycles(session, task_names: tuple[str, ...], local_date):
    statement = text(
        """
        SELECT c.id, c.task_id, t.name, l.obligation_local_date
        FROM content_mix_cycles c
        JOIN tasks t ON t.id = c.task_id
        JOIN task_day_ledgers l ON l.id = c.task_day_ledger_id
        WHERE t.type = 'group_ai_chat'
          AND t.name IN :task_names
          AND l.obligation_local_date < :local_date
          AND (
              c.settlement_status <> 'settled'
              OR EXISTS (
                  SELECT 1
                  FROM content_mix_cycle_slots s
                  WHERE s.cycle_id = c.id
                    AND s.slot_state IN ('unmaterialized', 'replan_pending')
              )
          )
        ORDER BY l.obligation_local_date, c.id
        FOR UPDATE OF c
        """
    ).bindparams(bindparam("task_names", expanding=True))
    return list(
        session.execute(
            statement,
            {"task_names": task_names, "local_date": local_date},
        ).mappings()
    )


def _linked_fact_ids(session, cycle_ids: tuple[str, ...]) -> dict:
    if not cycle_ids:
        return {"actions": (), "attempts": (), "remote_attempts": ()}
    statement = text(
        """
        SELECT DISTINCT a.id AS action_id, e.id AS attempt_id,
               COALESCE(e.remote_message_id, '') AS remote_message_id
        FROM content_mix_cycle_slots s
        LEFT JOIN actions a ON a.content_mix_cycle_slot_id = s.id
        LEFT JOIN execution_attempts e ON e.action_id = a.id
        WHERE s.cycle_id IN :cycle_ids
        """
    ).bindparams(bindparam("cycle_ids", expanding=True))
    rows = list(
        session.execute(
            statement, {"cycle_ids": cycle_ids}
        ).mappings()
    )
    return {
        "actions": tuple({str(row["action_id"]) for row in rows if row["action_id"]}),
        "attempts": tuple({str(row["attempt_id"]) for row in rows if row["attempt_id"]}),
        "remote_attempts": tuple(
            {
                str(row["attempt_id"])
                for row in rows
                if row["attempt_id"] and row["remote_message_id"]
            }
        ),
    }


def _fact_counts(fact_ids: dict) -> dict[str, int]:
    return {key: len(values) for key, values in fact_ids.items()}


def _contract_ids(session, cycle_ids: tuple[str, ...]) -> tuple[str, ...]:
    if not cycle_ids:
        return ()
    return tuple(
        str(value)
        for value in session.scalars(
            text(
                """
                SELECT id
                FROM content_mix_contracts
                WHERE split_part(content_mix_scope_key, ':', 4) IN :cycle_ids
                """
            ).bindparams(bindparam("cycle_ids", expanding=True)),
            {"cycle_ids": cycle_ids},
        )
    )


def _delete_ids(session, table: str, ids: tuple[str, ...]) -> int:
    if not ids:
        return 0
    statement = text(
        f"DELETE FROM {table} WHERE id IN :ids RETURNING id"
    ).bindparams(bindparam("ids", expanding=True))
    return len(list(session.scalars(statement, {"ids": ids})))


def _existing_count(session, table: str, ids: tuple[str, ...]) -> int:
    if not ids:
        return 0
    statement = text(
        f"SELECT COUNT(*) FROM {table} WHERE id IN :ids"
    ).bindparams(bindparam("ids", expanding=True))
    return int(session.scalar(statement, {"ids": ids}) or 0)


def _existing_remote_attempt_count(
    session,
    ids: tuple[str, ...],
) -> int:
    if not ids:
        return 0
    statement = text(
        """
        SELECT COUNT(*)
        FROM execution_attempts
        WHERE id IN :ids
          AND COALESCE(remote_message_id, '') <> ''
        """
    ).bindparams(bindparam("ids", expanding=True))
    return int(session.scalar(statement, {"ids": ids}) or 0)


def delete_stale_planning(
    task_names: tuple[str, ...],
    *,
    apply: bool,
) -> dict:
    local_date = datetime.now(LOCAL_TIMEZONE).date()
    with SessionLocal() as session:
        cycles = _target_cycles(session, task_names, local_date)
        cycle_ids = tuple(str(row["id"]) for row in cycles)
        fact_ids = _linked_fact_ids(session, cycle_ids)
        contract_ids = _contract_ids(session, cycle_ids)
        result = {
            "mode": "apply" if apply else "preview",
            "local_date": local_date,
            "cycles": [dict(row) for row in cycles],
            "contracts": len(contract_ids),
            "linked_facts_before": _fact_counts(fact_ids),
        }
        if not apply:
            session.rollback()
            return result
        result["deleted_contracts"] = _delete_ids(
            session, "content_mix_contracts", contract_ids
        )
        result["deleted_cycles"] = _delete_ids(
            session, "content_mix_cycles", cycle_ids
        )
        result["linked_facts_after"] = {
            "actions": _existing_count(session, "actions", fact_ids["actions"]),
            "attempts": _existing_count(
                session, "execution_attempts", fact_ids["attempts"]
            ),
            "remote_attempts": _existing_remote_attempt_count(
                session, fact_ids["remote_attempts"]
            ),
        }
        session.commit()
        return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Delete stale AI group planning while preserving send facts."
    )
    parser.add_argument("--task-name", action="append", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    result = delete_stale_planning(
        tuple(args.task_name),
        apply=bool(args.apply),
    )
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

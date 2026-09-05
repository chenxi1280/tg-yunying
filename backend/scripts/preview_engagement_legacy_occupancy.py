"""Read-only cutover inventory; this command never reserves or replays calls."""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import sys
from time import monotonic

from sqlalchemy import event, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import SessionLocal
from app.services.task_center.engagement_legacy_occupancy import (
    LegacyOccupancyScope, read_legacy_attempt_occupancy)


QUERY_TIMEOUT_SECONDS = 12


def _arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", type=int, required=True)
    parser.add_argument("--task-day", type=date.fromisoformat, required=True)
    parser.add_argument("--account-ids-file", type=Path, required=True,
        help="JSON array of exact approved account IDs, not a dynamic pool selector")
    parser.add_argument("--include-attempts", action="store_true")
    return parser.parse_args()


def _scope(args):
    ids = json.loads(args.account_ids_file.read_text())
    if not isinstance(ids, list) or any(type(item) is not int for item in ids):
        raise ValueError("account_ids_file_must_contain_integer_array")
    return LegacyOccupancyScope(tenant_id=args.tenant_id,
        account_ids=tuple(sorted(ids)), task_day=args.task_day)


def _summaries(rows, task_day):
    counts = Counter((row.task_id, row.account_id, row.action_class,
        row.original_task_day == task_day, row.call_day == task_day,
        row.remote_inflight, bool(row.issues)) for row in rows)
    return [dict(task_id=key[0], account_id=key[1], action_class=key[2],
        original_task_day_match=key[3], actual_call_day_match=key[4],
        remote_inflight=key[5], evidence_issue=key[6], attempt_count=count)
        for key, count in sorted(counts.items())]


def _read(scope):
    operations = []

    def record(_connection, _cursor, statement, _params, _context, _many):
        operations.append(statement.split()[0].upper())

    with SessionLocal() as session:
        session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        session.execute(text(f"SET LOCAL statement_timeout = '{QUERY_TIMEOUT_SECONDS}s'"))
        observed_at = session.scalar(text("SELECT now()"))
        connection = session.connection()
        event.listen(connection, "before_cursor_execute", record)
        started = monotonic()
        try:
            rows = read_legacy_attempt_occupancy(session, scope)
        finally:
            event.remove(connection, "before_cursor_execute", record)
        elapsed = round(monotonic() - started, 4)
    return rows, dict(observed_at=observed_at, query_seconds=elapsed, sql_operations=operations)


def main():
    args = _arguments()
    scope = _scope(args)
    rows, measurement = _read(scope)
    records = [asdict(row) for row in rows]
    digest = hashlib.sha256(json.dumps(records, sort_keys=True, default=str).encode()).hexdigest()
    report = dict(mode="preview_only", read_only=True, deployed_sha=os.getenv("RELEASE_SHA"),
        tenant_id=scope.tenant_id, task_day=scope.task_day, account_count=len(scope.account_ids),
        account_ids_hash=hashlib.sha256(json.dumps(scope.account_ids).encode()).hexdigest(),
        inventory_hash=digest, attempt_count=len(rows), measurement=measurement,
        summaries=_summaries(rows, scope.task_day),
        issues=[dict(attempt_id=row.attempt_id, codes=row.issues) for row in rows if row.issues],
        business_status="not_an_e4_report", admission_status="not_applied")
    if args.include_attempts:
        report["attempts"] = records
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, default=str))


if __name__ == "__main__":
    main()

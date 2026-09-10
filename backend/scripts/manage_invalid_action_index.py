"""Remove only the verified leftover concurrent Action index, with audit."""
import argparse
import hashlib
import json
import os
from pathlib import Path

from sqlalchemy import text

from app.database import engine, SessionLocal
from app.models import AuditLog
from app.services.task_center.runtime_storage_maintenance import MaintenanceContext


TARGET = "ix_actions_hard_hourly_history_scheduled_ccnew"
FORMAL = "ix_actions_hard_hourly_history_scheduled"
LOCK_KEY = 20_260_910_181
INDEX_QUERY = """
SELECT c.oid,c.relname,i.indisvalid,i.indisready,i.indisprimary,i.indisunique,i.indisreplident,
       pg_get_indexdef(c.oid) AS definition
FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_index i ON i.indexrelid=c.oid
WHERE n.nspname='public' AND c.relname IN (:target,:formal) AND i.indrelid='public.actions'::regclass
ORDER BY c.relname
"""


def snapshot(connection):
    return [dict(row) for row in connection.execute(text(INDEX_QUERY), {"target": TARGET, "formal": FORMAL}).mappings()]


def fingerprint(rows):
    return hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()


def validate(rows, expected):
    if fingerprint(rows) != expected:
        raise ValueError("invalid_index_manifest_drift")
    by_name = {row["relname"]: row for row in rows}
    if set(by_name) != {TARGET, FORMAL}:
        raise ValueError("invalid_index_pair_missing")
    target, formal = by_name[TARGET], by_name[FORMAL]
    if target["indisvalid"] or not target["indisready"] or not formal["indisvalid"] or not formal["indisready"]:
        raise ValueError("invalid_index_state_changed")
    if any(target[field] for field in ("indisprimary", "indisunique", "indisreplident")):
        raise ValueError("invalid_index_is_business_constraint")
    if target["definition"].replace(TARGET, "index", 1) != formal["definition"].replace(FORMAL, "index", 1):
        raise ValueError("invalid_index_definition_mismatch")


def record_audit(context, *, phase, rows):
    with SessionLocal.begin() as session:
        session.add(AuditLog(actor=context.actor, action="invalid_action_index_" + phase,
            target_type="database_index", target_id=TARGET,
            detail=json.dumps({"release_sha": context.current_release_sha,
                               "approval_ref": context.approval_ref, "indexes": rows}, sort_keys=True)))


def apply(expected, context):
    context.validate()
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        if not connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": LOCK_KEY}):
            raise RuntimeError("invalid_index_maintenance_busy")
        try:
            return _drop(connection, expected, context)
        finally:
            connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": LOCK_KEY})


def _drop(connection, expected, context):
    rows = snapshot(connection)
    validate(rows, expected)
    active = connection.scalar(text(
        "SELECT count(*) FROM pg_stat_progress_create_index WHERE index_relid IN (:target,:formal)"),
        {"target": next(row["oid"] for row in rows if row["relname"] == TARGET),
         "formal": next(row["oid"] for row in rows if row["relname"] == FORMAL)})
    if active:
        raise RuntimeError("invalid_index_rebuild_in_progress")
    record_audit(context, phase="started", rows=rows)
    connection.execute(text("SET lock_timeout='2s'"))
    connection.execute(text("SET statement_timeout='30s'"))
    connection.execute(text("DROP INDEX CONCURRENTLY public.ix_actions_hard_hourly_history_scheduled_ccnew"))
    after = snapshot(connection)
    if after != [row for row in rows if row["relname"] == FORMAL]:
        raise RuntimeError("invalid_index_readback_mismatch")
    record_audit(context, phase="verified", rows=after)
    return {"persisted_verified": True, "indexes": after}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("preview", "apply", "readback"))
    parser.add_argument("--expected-fingerprint", default="")
    parser.add_argument("--expected-release-sha", required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--approval-ref", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    context = MaintenanceContext(os.environ.get("APP_ENV", ""), args.expected_release_sha,
        os.environ.get("RELEASE_SHA", ""), args.actor, args.approval_ref)
    context.validate()
    with args.output.open("x") as output:
        if args.mode == "apply":
            result = apply(args.expected_fingerprint, context)
        else:
            with engine.connect() as connection:
                connection.execute(text("SET TRANSACTION READ ONLY"))
                rows = snapshot(connection)
            result = {"indexes": rows, "fingerprint": fingerprint(rows)}
        json.dump(result, output, sort_keys=True)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()

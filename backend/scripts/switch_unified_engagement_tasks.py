"""Preview, retire, clean and activate fresh unified Tasks from one exact audited mapping."""
import argparse
import json
import os
from pathlib import Path
import sys

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import SessionLocal
from app.services.task_center.engagement_direct_cutover import (
    CutoverOperation, activate_cutover, preview_cutover, retire_cutover, verify_retirement,
)
from app.services.task_center.engagement_cutover_capacity import preview_cutover_capacity
from app.services.task_center.engagement_retirement_cleanup import (
    CLEANUP_BATCH_SIZE, cleanup_cutover_batch, cleanup_remaining, require_cutover_cleanup,
)
from app.services.task_center.service import _new_task, start_task_in_transaction


DEFAULT_STATEMENT_TIMEOUT_SECONDS = 120
DEFAULT_LOCK_TIMEOUT_SECONDS = 5


def _arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("preview", "retire", "cleanup", "activate", "readback"), required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--expected-deployed-sha", required=True)
    parser.add_argument("--actor")
    parser.add_argument("--audit-reference")
    parser.add_argument("--batch-size", type=int, default=CLEANUP_BATCH_SIZE)
    parser.add_argument("--statement-timeout", type=int, default=DEFAULT_STATEMENT_TIMEOUT_SECONDS)
    parser.add_argument("--lock-timeout", type=int, default=DEFAULT_LOCK_TIMEOUT_SECONDS)
    return parser.parse_args()


def _transaction(session, *, readonly, statement_timeout=DEFAULT_STATEMENT_TIMEOUT_SECONDS, lock_timeout=DEFAULT_LOCK_TIMEOUT_SECONDS):
    if readonly:
        session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
    session.execute(text(f"SET LOCAL statement_timeout = '{statement_timeout}s'"))
    session.execute(text(f"SET LOCAL lock_timeout = '{lock_timeout}s'"))


def _build(session, old, payload):
    task = _new_task(session, old.tenant_id, old.type, payload)
    task.created_by_user_id = old.created_by_user_id
    return task


def _readback(session, receipt):
    old, new = verify_retirement(session, receipt)
    return {"mapping": receipt["mapping"], "old_retired": len(old),
        "new_tasks": [{"id": task.id, "type": task.type, "status": task.status,
            "epoch": task.task_lifecycle_epoch} for task in new], "cleanup_remaining": cleanup_remaining(session, receipt)}


def _execute(session, args, payload, *, operation):
    if args.mode == "preview":
        if payload.get("deployed_sha") != operation.deployed_sha:
            raise ValueError("engagement_cutover_spec_deployed_sha_changed")
        preview = preview_cutover(session, payload)
        return {**preview, "capacity_preview": preview_cutover_capacity(session, preview)}
    if args.mode == "retire":
        return retire_cutover(session, payload, operation, create_replacement=_build)
    if args.mode == "cleanup":
        return cleanup_cutover_batch(session, payload, operation, batch_size=args.batch_size)
    if args.mode == "activate":
        return activate_cutover(session, payload, operation, start_replacement=start_task_in_transaction,
            require_cleanup=require_cutover_cleanup)
    return _readback(session, payload)


def _save(path, result):
    if path is None:
        return
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w") as output:
        json.dump(result, output, ensure_ascii=False, sort_keys=True, default=str)
        output.write("\n")


def main():
    args = _arguments()
    deployed = os.getenv("RELEASE_SHA")
    if deployed != args.expected_deployed_sha:
        raise ValueError("engagement_cutover_runtime_sha_mismatch")
    if args.mode in {"preview", "retire"} and args.output is None:
        raise ValueError("engagement_cutover_manifest_output_required")
    payload = json.loads(args.input.read_text())
    operation = CutoverOperation(args.actor or "", args.audit_reference or "", deployed)
    with SessionLocal() as session:
        _transaction(session, readonly=args.mode in {"preview", "readback"},
                     statement_timeout=args.statement_timeout, lock_timeout=args.lock_timeout)
        result = _execute(session, args, payload, operation=operation)
        if args.mode not in {"preview", "readback"}:
            session.commit()
    _save(args.output, result)
    if args.mode in {"retire", "activate", "cleanup"}:
        receipt = result if args.mode == "retire" else payload
        with SessionLocal() as verification:
            _transaction(verification, readonly=True,
                         statement_timeout=args.statement_timeout, lock_timeout=args.lock_timeout)
            result = {**result, "readback": _readback(verification, receipt)}
    if args.mode == "preview":
        result = {"state_hash": result["state_hash"], "task_count": len(result["state"]["tasks"]),
            "shared_class_budgets": result["capacity_preview"]["shared_class_budgets"]}
    print(json.dumps({"mode": args.mode, "deployed_sha": deployed, **result}, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()

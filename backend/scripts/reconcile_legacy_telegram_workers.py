"""Acknowledge exact legacy transports from original Docker PID 1 exit evidence."""
import argparse
import json
import os
from pathlib import Path
import sys

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import SessionLocal
from app.services.task_center.telegram_worker_exit_reconcile import (
    WorkerExitOperation, apply_worker_exits, preview_worker_exits, verify_worker_exits,
)


STATEMENT_TIMEOUT_SECONDS = 20
LOCK_TIMEOUT_SECONDS = 2


def _arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("preview", "apply", "readback"), required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-deployed-sha", required=True)
    parser.add_argument("--actor", default="")
    parser.add_argument("--audit-reference", default="")
    return parser.parse_args()


def _transaction(session, *, readonly):
    if readonly:
        session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
    session.execute(text(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT_SECONDS}s'"))
    session.execute(text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT_SECONDS}s'"))


def _execute(session, args, payload):
    if args.mode == "preview":
        if payload.get("deployed_sha") != args.expected_deployed_sha:
            raise ValueError("worker_exit_spec_deployed_sha_changed")
        return preview_worker_exits(session, payload)
    if args.mode == "readback":
        return verify_worker_exits(session, payload)
    operation = WorkerExitOperation(args.actor, args.audit_reference, args.expected_deployed_sha)
    return apply_worker_exits(session, payload, operation)


def _save(path, result):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w") as output:
        json.dump(result, output, ensure_ascii=False, sort_keys=True)
        output.write("\n")


def main():
    args = _arguments()
    if os.getenv("RELEASE_SHA") != args.expected_deployed_sha:
        raise ValueError("worker_exit_runtime_sha_mismatch")
    payload = json.loads(args.input.read_text())
    with SessionLocal() as session:
        _transaction(session, readonly=args.mode != "apply")
        result = _execute(session, args, payload)
        if args.mode == "apply":
            session.commit()
    _save(args.output, result)
    if args.mode == "apply":
        with SessionLocal() as verification:
            _transaction(verification, readonly=True)
            summary = verify_worker_exits(verification, result)
    elif args.mode == "preview":
        summary = {"state_hash": result["state_hash"], "attempt_count": len(result["state"]["attempts"])}
    else:
        summary = result
    print(json.dumps({"mode": args.mode, "deployed_sha": args.expected_deployed_sha, **summary}))


if __name__ == "__main__":
    main()

"""Preview/apply/readback exact never-called reaction backlog replacement."""
import argparse
import json
import os
from pathlib import Path

from sqlalchemy import text

from app.database import SessionLocal
from app.services.task_center.reaction_backlog_replan import (
    BacklogReplanOperation, apply_reaction_backlog, verify_reaction_backlog,
)
from app.services.task_center.reaction_backlog_snapshot import preview_reaction_backlog


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
    scope = payload if args.mode == "preview" else payload["scope"]
    if scope["deployed_sha"] != args.expected_deployed_sha:
        raise ValueError("reaction_backlog_spec_sha_mismatch")
    if args.mode == "preview":
        return preview_reaction_backlog(session, payload)
    if args.mode == "readback":
        return verify_reaction_backlog(session, payload)
    return apply_reaction_backlog(session, payload,
        BacklogReplanOperation(args.actor, args.audit_reference, args.expected_deployed_sha))


def _save(path, result):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w") as output:
        json.dump(result, output, ensure_ascii=False, sort_keys=True)
        output.write("\n")


def main():
    args = _arguments()
    if os.getenv("RELEASE_SHA") != args.expected_deployed_sha:
        raise ValueError("reaction_backlog_runtime_sha_mismatch")
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
            summary = verify_reaction_backlog(verification, result)
    elif args.mode == "preview":
        summary = {"state_hash": result["state_hash"], "candidate_count": len(result["state"]["actions"])}
    else:
        summary = result
    print(json.dumps({"mode": args.mode, "deployed_sha": args.expected_deployed_sha, **summary}))


if __name__ == "__main__":
    main()

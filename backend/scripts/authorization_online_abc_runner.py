from __future__ import annotations

import argparse
import json
import os

from app.database import SessionLocal
from app.services.authorization_dr import (
    online_abc_runner_status,
    resume_online_abc_batch,
    run_online_abc_batch,
)
from app.services.authorization_dr.contracts import AuthorizationDrError


def main() -> int:
    args = _parser().parse_args()
    try:
        with SessionLocal() as session:
            result = _execute(session, args)
    except Exception as exc:
        result = _error_out(exc)
        print("AUTHORIZATION_ONLINE_ABC_RUNNER=" + json.dumps(result, sort_keys=True), flush=True)
        return 1
    print("AUTHORIZATION_ONLINE_ABC_RUNNER=" + json.dumps(result, sort_keys=True), flush=True)
    if args.mode == "status":
        return 0
    return 1 if result["batch"]["status"] == "stopped" else 0


def _execute(session, args) -> dict:
    if args.mode == "status":
        return online_abc_runner_status(session, args.batch_id)
    if args.mode == "resume":
        resume_online_abc_batch(
            session,
            args.batch_id,
            requested_by=args.requested_by,
            approved_by=args.approved_by,
            approval_ref=args.approval_ref,
            runtime_release_sha=os.getenv("RELEASE_SHA", ""),
        )
    return run_online_abc_batch(
        session,
        args.batch_id,
        requested_by=args.requested_by,
        approved_by=args.approved_by,
        approval_ref=args.approval_ref,
        runtime_release_sha=os.getenv("RELEASE_SHA", ""),
        poll_seconds=args.poll_seconds,
    )


def _error_out(exc: Exception) -> dict:
    code = exc.code if isinstance(exc, AuthorizationDrError) else type(exc).__name__
    return {"status": "error", "code": code, "message": str(exc)}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one approved online ABC batch without GitHub Actions")
    parser.add_argument("--mode", choices=("status", "run", "resume"), required=True)
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--requested-by", default="")
    parser.add_argument("--approved-by", default="")
    parser.add_argument("--approval-ref", default="")
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    return parser


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import argparse
import json
import os
import time

from sqlalchemy import text

from app.database import SessionLocal
from app.services.authorization_dr.online_abc_sweep import (
    apply_online_abc_sweep_start,
    online_abc_sweep_status,
    preview_online_abc_sweep_start,
    pause_online_abc_sweep_for_error,
    readback_online_abc_sweep_start,
    run_online_abc_sweep_once,
)

SWEEP_ADVISORY_LOCK_KEY = 0x4142435357454550


def main() -> int:
    args = _parser().parse_args()
    if args.mode == "worker":
        return _worker(args)
    with SessionLocal() as session:
        result = _execute(session, args)
    print("AUTHORIZATION_ONLINE_ABC_SWEEP=" + json.dumps(result, sort_keys=True), flush=True)
    return 0


def _worker(args) -> int:
    if args.worker_interval_seconds <= 0:
        raise ValueError("online_abc_sweep_worker_interval_must_be_positive")
    with SessionLocal() as lock_session:
        if not _acquire_worker_lock(lock_session):
            raise RuntimeError("online_abc_sweep_worker_already_running")
        lock_session.commit()
        while True:
            lock_session.execute(text("SELECT 1"))
            lock_session.commit()
            try:
                with SessionLocal() as session:
                    result = run_online_abc_sweep_once(
                        session, runtime_release_sha=_release_sha(), poll_seconds=args.poll_seconds,
                    )
                print("AUTHORIZATION_ONLINE_ABC_SWEEP_WORKER=" + json.dumps(result, sort_keys=True), flush=True)
            except Exception as exc:
                blocker = type(exc).__name__
                try:
                    with SessionLocal() as error_session:
                        paused = pause_online_abc_sweep_for_error(error_session, blocker)
                    print(
                        "AUTHORIZATION_ONLINE_ABC_SWEEP_WORKER_ERROR="
                        + json.dumps({"blocker": blocker, "paused": paused}, sort_keys=True),
                        flush=True,
                    )
                except Exception as pause_exc:
                    print(
                        "AUTHORIZATION_ONLINE_ABC_SWEEP_WORKER_FATAL="
                        + repr(pause_exc), flush=True,
                    )
                return 1
            time.sleep(args.worker_interval_seconds)


def _acquire_worker_lock(session) -> bool:
    if session.bind.dialect.name != "postgresql":
        raise RuntimeError("online_abc_sweep_worker_requires_postgresql")
    return bool(session.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": SWEEP_ADVISORY_LOCK_KEY}))


def _execute(session, args) -> dict:
    if args.mode == "status":
        return online_abc_sweep_status(session, args.batch_id)
    if args.mode == "readback":
        return readback_online_abc_sweep_start(
            session, args.batch_id, idempotency_key=args.idempotency_key,
        )
    if not args.until_exhausted:
        raise ValueError("online_abc_sweep_requires_until_exhausted")
    kwargs = {
        "runtime_release_sha": _release_sha(),
        "idempotency_key": args.idempotency_key,
        "requested_by": args.requested_by,
        "approved_by": args.approved_by,
        "approval_ref": args.approval_ref,
    }
    if args.mode == "preview":
        return preview_online_abc_sweep_start(session, args.batch_id, **kwargs)
    return apply_online_abc_sweep_start(
        session, args.batch_id, expected_fingerprint=args.expected_fingerprint, **kwargs,
    )


def _release_sha() -> str:
    return os.getenv("RELEASE_SHA", "")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Start or observe the durable online ABC one-shot sweep")
    parser.add_argument("--mode", choices=("preview", "sweep", "apply", "readback", "status", "worker"), required=True)
    parser.add_argument("--batch-id", default="")
    parser.add_argument("--idempotency-key", default="")
    parser.add_argument("--expected-fingerprint", default="")
    parser.add_argument("--requested-by", default="")
    parser.add_argument("--approved-by", default="")
    parser.add_argument("--approval-ref", default="")
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument("--worker-interval-seconds", type=float, default=2.0)
    parser.add_argument("--until-exhausted", action="store_true")
    return parser


if __name__ == "__main__":
    raise SystemExit(main())

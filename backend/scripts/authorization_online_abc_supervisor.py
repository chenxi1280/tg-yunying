from __future__ import annotations

import argparse
import json
import os
import time

from sqlalchemy import text

from app.database import SessionLocal
from app.services.authorization_dr.online_abc_deferred_recovery import (
    pause_deferred_recovery_for_error,
    run_deferred_recovery_once,
)
from app.services.authorization_dr.online_abc_sweep import (
    pause_online_abc_sweep_for_error,
    run_online_abc_sweep_once,
)
from app.services.authorization_dr.online_abc_post_login import run_post_login_exact_once


SUPERVISOR_LOCK_KEY = 0x4142435357454550


def main() -> int:
    args = _parser().parse_args()
    if args.worker_interval_seconds <= 0:
        raise ValueError("abc_supervisor_worker_interval_must_be_positive")
    if args.poll_seconds <= 0:
        raise ValueError("abc_supervisor_poll_interval_must_be_positive")
    with SessionLocal() as lock_session:
        if not _acquire_lock(lock_session):
            raise RuntimeError("abc_supervisor_already_running")
        lock_session.commit()
        return _loop(args, lock_session)


def _loop(args, lock_session) -> int:
    while True:
        lock_session.execute(text("SELECT 1"))
        lock_session.commit()
        result = _run_once(args)
        print("AUTHORIZATION_ONLINE_ABC_SUPERVISOR=" + json.dumps(result, sort_keys=True), flush=True)
        time.sleep(args.worker_interval_seconds)


def _run_once(args) -> dict:
    try:
        with SessionLocal() as session:
            sweep = run_online_abc_sweep_once(
                session, runtime_release_sha=_release_sha(), poll_seconds=args.poll_seconds,
            )
        if sweep.get("status") != "idle":
            return {"lane": "full_sweep", "result": sweep}
        with SessionLocal() as session:
            recovery = run_deferred_recovery_once(session, runtime_release_sha=_release_sha())
    except Exception as exc:
        return _pause_after_error(type(exc).__name__)
    if recovery.get("status") != "idle":
        return {"lane": "deferred_recovery", "result": recovery}
    try:
        with SessionLocal() as session:
            post_login = run_post_login_exact_once(
                session,
                runtime_release_sha=_release_sha(),
                poll_seconds=args.poll_seconds,
            )
    except Exception as exc:
        return {"lane": "post_login_exact_error", "blocker": type(exc).__name__}
    return {"lane": "post_login_exact", "result": post_login}


def _pause_after_error(blocker: str) -> dict:
    paused = {}
    with SessionLocal() as session:
        paused["full_sweep"] = pause_online_abc_sweep_for_error(session, blocker)
    with SessionLocal() as session:
        paused["deferred_recovery"] = pause_deferred_recovery_for_error(session, blocker)
    return {"lane": "error", "blocker": blocker, "paused": paused}


def _acquire_lock(session) -> bool:
    if session.bind.dialect.name != "postgresql":
        raise RuntimeError("abc_supervisor_requires_postgresql")
    return bool(session.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": SUPERVISOR_LOCK_KEY}))


def _release_sha() -> str:
    return os.getenv("RELEASE_SHA", "")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Durable ABC full-sweep, deferred-recovery, and post-login supervisor"
    )
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument("--worker-interval-seconds", type=float, default=2.0)
    return parser


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import argparse
import json
import os

from app.database import SessionLocal
from app.services.authorization_dr.online_abc_deferred_recovery import (
    apply_deferred_recovery_start,
    canonical_deferred_manifest,
    deferred_recovery_status,
    pause_deferred_recovery_for_error,
    preview_deferred_recovery_start,
    readback_deferred_recovery_start,
)


def main() -> int:
    args = _parser().parse_args()
    with SessionLocal() as session:
        result = _execute(session, args)
    print("AUTHORIZATION_ONLINE_ABC_DEFERRED_RECOVERY=" + json.dumps(result, sort_keys=True), flush=True)
    return 0


def _execute(session, args) -> dict:
    if args.mode == "status":
        return deferred_recovery_status(session, args.batch_id)
    if args.mode == "manifest":
        return canonical_deferred_manifest(session, args.batch_id, runtime_release_sha=_release_sha())
    if args.mode == "readback":
        return readback_deferred_recovery_start(
            session, args.batch_id, idempotency_key=args.idempotency_key,
        )
    if args.mode == "pause":
        return pause_deferred_recovery_for_error(session, args.blocker)
    if not args.until_exhausted:
        raise ValueError("deferred_recovery_requires_until_exhausted")
    kwargs = {
        "runtime_release_sha": _release_sha(),
        "idempotency_key": args.idempotency_key,
        "expected_deferred_count": args.expected_deferred_count,
        "requested_by": args.requested_by,
        "approved_by": args.approved_by,
        "approval_ref": args.approval_ref,
    }
    if args.mode == "preview":
        return preview_deferred_recovery_start(session, args.batch_id, **kwargs)
    return apply_deferred_recovery_start(
        session, args.batch_id, expected_fingerprint=args.expected_fingerprint, **kwargs,
    )


def _release_sha() -> str:
    return os.getenv("RELEASE_SHA", "")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Start or inspect ABC deferred recovery sweep")
    parser.add_argument("--mode", choices=("manifest", "preview", "apply", "pause", "readback", "status"), required=True)
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--idempotency-key", default="")
    parser.add_argument("--expected-fingerprint", default="")
    parser.add_argument("--expected-deferred-count", type=int, default=0)
    parser.add_argument("--requested-by", default="")
    parser.add_argument("--approved-by", default="")
    parser.add_argument("--approval-ref", default="")
    parser.add_argument("--blocker", default="operator_pause")
    parser.add_argument("--until-exhausted", action="store_true")
    return parser


if __name__ == "__main__":
    raise SystemExit(main())

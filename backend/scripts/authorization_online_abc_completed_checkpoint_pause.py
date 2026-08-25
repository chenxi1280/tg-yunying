from __future__ import annotations

import argparse
import json
import os

from app.database import SessionLocal
from app.services.authorization_dr.online_abc_completed_checkpoint_pause import (
    apply_completed_checkpoint_pause,
    preview_completed_checkpoint_pause,
    readback_completed_checkpoint_pause,
)


def main() -> int:
    args = _parser().parse_args()
    with SessionLocal() as session:
        result = _execute(session, args)
    print("AUTHORIZATION_ONLINE_ABC_COMPLETED_CHECKPOINT_PAUSE=" + json.dumps(result, sort_keys=True))
    return 0


def _execute(session, args) -> dict:
    if args.mode == "readback":
        return readback_completed_checkpoint_pause(
            session, args.batch_id, args.account_id, idempotency_key=args.idempotency_key,
        )
    common = {
        "runtime_release_sha": os.getenv("RELEASE_SHA", ""),
        "idempotency_key": args.idempotency_key,
        "requested_by": args.requested_by,
        "approved_by": args.approved_by,
        "approval_ref": args.approval_ref,
        "interruption_ref": args.interruption_ref,
    }
    if args.mode == "preview":
        return preview_completed_checkpoint_pause(
            session, args.batch_id, args.account_id, **common,
        )
    return apply_completed_checkpoint_pause(
        session, args.batch_id, args.account_id,
        expected_fingerprint=args.expected_fingerprint, **common,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Pause a release-interrupted completed ABC checkpoint")
    parser.add_argument("--mode", choices=("preview", "apply", "readback"), required=True)
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--account-id", type=int, required=True)
    parser.add_argument("--idempotency-key", required=True)
    parser.add_argument("--expected-fingerprint", default="")
    parser.add_argument("--requested-by", default="")
    parser.add_argument("--approved-by", default="")
    parser.add_argument("--approval-ref", default="")
    parser.add_argument("--interruption-ref", default="")
    return parser


if __name__ == "__main__":
    raise SystemExit(main())

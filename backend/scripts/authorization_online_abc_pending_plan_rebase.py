from __future__ import annotations

import argparse
import json

from app.database import SessionLocal
from app.services.authorization_dr.online_abc_pending_plan_rebase import (
    apply_pending_plan_rebase,
    preview_pending_plan_rebase,
)


def main() -> int:
    args = _parser().parse_args()
    common = {
        "expected_target_count": args.expected_target_count,
        "idempotency_key": args.idempotency_key,
        "requested_by": args.requested_by,
        "approved_by": args.approved_by,
        "approval_ref": args.approval_ref,
    }
    with SessionLocal() as session:
        if args.mode == "preview":
            result = preview_pending_plan_rebase(session, args.batch_id, **common)
        else:
            result = apply_pending_plan_rebase(
                session,
                args.batch_id,
                expected_fingerprint=args.expected_fingerprint,
                **common,
            )
    print("AUTHORIZATION_ONLINE_ABC_PENDING_PLAN_REBASE=" + json.dumps(result, sort_keys=True))
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Rebase untouched pending ABC B/C plans")
    parser.add_argument("--mode", choices=("preview", "apply"), required=True)
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--expected-target-count", type=int, required=True)
    parser.add_argument("--idempotency-key", required=True)
    parser.add_argument("--expected-fingerprint", default="")
    parser.add_argument("--requested-by", required=True)
    parser.add_argument("--approved-by", required=True)
    parser.add_argument("--approval-ref", required=True)
    return parser


if __name__ == "__main__":
    raise SystemExit(main())

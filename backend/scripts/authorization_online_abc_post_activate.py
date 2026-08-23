from __future__ import annotations

import argparse
import json

from app.database import SessionLocal
from app.services.authorization_dr.online_abc_post_activate import (
    apply_post_activate_rebase,
    preview_post_activate_rebase,
)


def main() -> int:
    args = _parser().parse_args()
    with SessionLocal() as session:
        if args.mode == "preview":
            result = preview_post_activate_rebase(
                session,
                args.batch_id,
                args.account_id,
                args.case_id,
                idempotency_key=args.idempotency_key,
            )
        else:
            result = apply_post_activate_rebase(
                session,
                args.batch_id,
                args.account_id,
                args.case_id,
                idempotency_key=args.idempotency_key,
                expected_fingerprint=args.expected_fingerprint,
                requested_by=args.requested_by,
                approved_by=args.approved_by,
                approval_ref=args.approval_ref,
            )
    print("AUTHORIZATION_ONLINE_ABC_POST_ACTIVATE=" + json.dumps(result, sort_keys=True))
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Rebase one frozen ABC item after verified local activation")
    parser.add_argument("--mode", choices=("preview", "apply"), required=True)
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--account-id", type=int, required=True)
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--idempotency-key", required=True)
    parser.add_argument("--expected-fingerprint", default="")
    parser.add_argument("--requested-by", default="")
    parser.add_argument("--approved-by", default="")
    parser.add_argument("--approval-ref", default="")
    return parser


if __name__ == "__main__":
    raise SystemExit(main())

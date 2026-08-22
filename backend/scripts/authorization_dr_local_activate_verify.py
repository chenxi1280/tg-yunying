from __future__ import annotations

import argparse
import json

from app.database import SessionLocal
from app.services.authorization_dr import (
    apply_local_activate_verification,
    preview_local_activate_verification,
)


def main() -> None:
    args = _parser().parse_args()
    with SessionLocal() as session:
        if args.mode == "preview":
            result = preview_local_activate_verification(
                session,
                args.tenant_id,
                args.account_id,
                args.case_id,
                idempotency_key=args.idempotency_key,
            )
        else:
            result = apply_local_activate_verification(
                session,
                args.tenant_id,
                args.account_id,
                args.case_id,
                idempotency_key=args.idempotency_key,
                expected_fingerprint=args.expected_fingerprint,
                requested_by=args.requested_by,
                approved_by=args.approved_by,
                approval_ref=args.approval_ref,
            )
    print("AUTHORIZATION_LOCAL_ACTIVATE_VERIFY=" + json.dumps(result, sort_keys=True), flush=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify a generation-fenced local activation by remote send")
    parser.add_argument("--mode", choices=("preview", "apply"), required=True)
    parser.add_argument("--tenant-id", type=int, required=True)
    parser.add_argument("--account-id", type=int, required=True)
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--idempotency-key", required=True)
    parser.add_argument("--expected-fingerprint", default="")
    parser.add_argument("--requested-by", default="")
    parser.add_argument("--approved-by", default="")
    parser.add_argument("--approval-ref", default="")
    return parser


if __name__ == "__main__":
    main()

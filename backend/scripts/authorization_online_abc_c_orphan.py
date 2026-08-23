from __future__ import annotations

import argparse
import json

from app.database import SessionLocal
from app.services.authorization_dr.c_orphan_recovery import (
    apply_c_orphan_recovery,
    preview_c_orphan_recovery,
)


def main() -> int:
    args = _parser().parse_args()
    with SessionLocal() as session:
        if args.mode == "preview":
            result = preview_c_orphan_recovery(session, args.batch_id, args.account_id)
        else:
            result = apply_c_orphan_recovery(
                session, args.batch_id, args.account_id,
                expected_fingerprint=args.expected_fingerprint,
                requested_by=args.requested_by, approved_by=args.approved_by,
                approval_ref=args.approval_ref,
            )
    print("AUTHORIZATION_ONLINE_ABC_C_ORPHAN=" + json.dumps(result, sort_keys=True))
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Reconcile and revoke one exact online ABC C orphan")
    parser.add_argument("--mode", choices=("preview", "apply"), required=True)
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--account-id", type=int, required=True)
    parser.add_argument("--expected-fingerprint", default="")
    parser.add_argument("--requested-by", default="")
    parser.add_argument("--approved-by", default="")
    parser.add_argument("--approval-ref", default="")
    return parser


if __name__ == "__main__":
    raise SystemExit(main())

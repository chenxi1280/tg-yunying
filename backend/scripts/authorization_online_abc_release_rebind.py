from __future__ import annotations

import argparse
import json
import os

from app.database import SessionLocal
from app.services.authorization_dr.online_abc_release_rebind import (
    apply_execution_release_rebind,
    preview_execution_release_rebind,
)


def main() -> int:
    args = _parser().parse_args()
    with SessionLocal() as session:
        common = {
            "runtime_release_sha": os.getenv("RELEASE_SHA", ""),
            "requested_by": args.requested_by,
            "approved_by": args.approved_by,
            "approval_ref": args.approval_ref,
        }
        if args.mode == "preview":
            result = preview_execution_release_rebind(session, args.batch_id, **common)
        else:
            result = apply_execution_release_rebind(
                session,
                args.batch_id,
                expected_fingerprint=args.expected_fingerprint,
                **common,
            )
    print("AUTHORIZATION_ONLINE_ABC_RELEASE_REBIND=" + json.dumps(result, sort_keys=True))
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Rebind a quiescent ABC batch to the current release")
    parser.add_argument("--mode", choices=("preview", "apply"), required=True)
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--expected-fingerprint", default="")
    parser.add_argument("--requested-by", required=True)
    parser.add_argument("--approved-by", required=True)
    parser.add_argument("--approval-ref", required=True)
    return parser


if __name__ == "__main__":
    raise SystemExit(main())

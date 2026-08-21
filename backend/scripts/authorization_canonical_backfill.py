from __future__ import annotations

import argparse
import json

from app.database import SessionLocal
from app.services.authorization_canonical_backfill import (
    apply_canonical_authorization_backfill,
    canonical_authorization_backfill_status,
    preview_canonical_authorization_backfill,
    preview_primary_qualification,
    qualify_primary_authorization,
)


def main() -> None:
    args = _parser().parse_args()
    with SessionLocal() as session:
        result = _execute(session, args)
    print("AUTHORIZATION_CANONICAL_BACKFILL=" + json.dumps(result, ensure_ascii=False, sort_keys=True))


def _execute(session, args) -> dict:
    if args.mode == "preview":
        return preview_canonical_authorization_backfill(session, args.tenant_id)
    if args.mode == "status":
        return canonical_authorization_backfill_status(session, args.tenant_id)
    if args.mode == "qualify-preview":
        return preview_primary_qualification(session, args.tenant_id, args.account_id)
    if args.mode == "qualify-apply":
        return qualify_primary_authorization(
            session,
            args.tenant_id,
            args.account_id,
            expected_fingerprint=args.expected_fingerprint,
            actor=args.approved_by,
            approval_ref=args.approval_ref,
        )
    return apply_canonical_authorization_backfill(
        session,
        args.tenant_id,
        expected_fingerprint=args.expected_fingerprint,
        requested_by=args.requested_by,
        approved_by=args.approved_by,
        approval_ref=args.approval_ref,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="DB-only canonical current A backfill")
    parser.add_argument(
        "--mode",
        choices=("preview", "apply", "status", "qualify-preview", "qualify-apply"),
        required=True,
    )
    parser.add_argument("--tenant-id", type=int, required=True)
    parser.add_argument("--account-id", type=int, default=0)
    parser.add_argument("--expected-fingerprint", default="")
    parser.add_argument("--requested-by", default="")
    parser.add_argument("--approved-by", default="")
    parser.add_argument("--approval-ref", default="")
    return parser


if __name__ == "__main__":
    main()

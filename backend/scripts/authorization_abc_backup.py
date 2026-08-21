from __future__ import annotations

import argparse
import json

from app.database import SessionLocal
from app.services.authorization_dr import (
    abc_canary_status,
    apply_abc_e4,
    apply_abc_backup,
    prepare_scoped_c_migration,
    preview_abc_e4,
    preview_abc_backup,
)


def main() -> None:
    args = _parser().parse_args()
    with SessionLocal() as session:
        result = _execute(session, args)
    print("AUTHORIZATION_ABC_BACKUP=" + json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)


def _execute(session, args) -> dict:
    if args.mode == "status":
        return abc_canary_status(session, args.tenant_id, args.account_id)
    if args.mode == "verify-preview":
        return preview_abc_e4(
            session,
            args.tenant_id,
            args.account_id,
            idempotency_key=args.idempotency_key,
        )
    if args.mode == "verify-apply":
        return apply_abc_e4(
            session,
            args.tenant_id,
            args.account_id,
            idempotency_key=args.idempotency_key,
            expected_fingerprint=args.expected_fingerprint,
            requested_by=args.requested_by,
            approved_by=args.approved_by,
            approval_ref=args.approval_ref,
        )
    if args.mode == "preview":
        return preview_abc_backup(
            session,
            args.tenant_id,
            args.account_id,
            idempotency_key=args.idempotency_key,
        )
    b_result = apply_abc_backup(
        session,
        args.tenant_id,
        args.account_id,
        idempotency_key=args.idempotency_key,
        expected_fingerprint=args.expected_fingerprint,
        requested_by=args.requested_by,
        approved_by=args.approved_by,
        approval_ref=args.approval_ref,
    )
    c_result = prepare_scoped_c_migration(
        session,
        args.tenant_id,
        args.account_id,
        idempotency_key=f"{args.idempotency_key}:c",
        requested_by=args.requested_by,
        approved_by=args.approved_by,
        approval_ref=args.approval_ref,
        runtime_image_sha=args.runtime_image_sha,
    )
    return {"b": b_result, "c": c_result}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Preview or execute one A-protected B/C backup operation; never switches A",
    )
    parser.add_argument(
        "--mode",
        choices=("preview", "apply", "status", "verify-preview", "verify-apply"),
        required=True,
    )
    parser.add_argument("--tenant-id", type=int, required=True)
    parser.add_argument("--account-id", type=int, required=True)
    parser.add_argument("--idempotency-key", default="")
    parser.add_argument("--expected-fingerprint", default="")
    parser.add_argument("--requested-by", default="")
    parser.add_argument("--approved-by", default="")
    parser.add_argument("--approval-ref", default="")
    parser.add_argument("--runtime-image-sha", default="")
    return parser


if __name__ == "__main__":
    main()

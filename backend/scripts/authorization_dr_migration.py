from __future__ import annotations

import argparse
import json

from app.database import SessionLocal
from app.services.authorization_dr import approve_migration_batch, migration_batch_out, preview_migration_batch


def main() -> None:
    args = _parser().parse_args()
    with SessionLocal() as session:
        result = _execute(session, args)
    print("AUTHORIZATION_DR_MIGRATION=" + json.dumps(result, ensure_ascii=False, sort_keys=True, default=str), flush=True)


def _execute(session, args) -> dict:
    if args.mode == "preview":
        ids = [int(item.strip()) for item in args.account_ids.split(",") if item.strip()]
        batch = preview_migration_batch(
            session,
            args.tenant_id,
            ids,
            idempotency_key=args.idempotency_key,
            actor=args.actor,
        )
        return migration_batch_out(session, batch.id, args.tenant_id)
    if args.mode == "approve":
        batch = approve_migration_batch(
            session,
            args.batch_id,
            expected_version=args.expected_version,
            approval_ref=args.approval_ref,
            actor=args.actor,
        )
        return migration_batch_out(session, batch.id, args.tenant_id)
    return migration_batch_out(session, args.batch_id, args.tenant_id)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("preview", "approve", "readback"), required=True)
    parser.add_argument("--tenant-id", type=int, required=True)
    parser.add_argument("--account-ids", default="")
    parser.add_argument("--idempotency-key", default="")
    parser.add_argument("--batch-id", default="")
    parser.add_argument("--expected-version", type=int, default=0)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--approval-ref", default="")
    return parser


if __name__ == "__main__":
    main()

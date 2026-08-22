from __future__ import annotations

import argparse
import json

from app.database import SessionLocal
from app.services.authorization_dr.online_abc import (
    apply_online_abc_batch,
    online_abc_batch_status,
    preview_online_abc_batch,
    start_next_online_abc_item,
    sync_online_abc_batch,
)
from app.services.authorization_dr.online_abc_rollout import accept_online_abc_observation
from app.services.authorization_dr.online_abc_manifest import (
    apply_full_online_abc_batch,
    preview_full_online_abc_batch,
)


def main() -> None:
    args = _parser().parse_args()
    with SessionLocal() as session:
        result = _execute(session, args)
    print("AUTHORIZATION_ONLINE_ABC=" + json.dumps(result, ensure_ascii=False, sort_keys=True))


def _execute(session, args) -> dict:
    if args.mode == "preview-full":
        return preview_full_online_abc_batch(
            session, args.tenant_id, idempotency_key=args.idempotency_key,
            deployed_release_sha=args.deployed_release_sha,
        )
    if args.mode == "apply-full":
        return apply_full_online_abc_batch(
            session, args.tenant_id, idempotency_key=args.idempotency_key,
            deployed_release_sha=args.deployed_release_sha,
            expected_fingerprint=args.expected_fingerprint,
            requested_by=args.requested_by, approved_by=args.approved_by,
            approval_ref=args.approval_ref,
        )
    if args.mode == "preview":
        return preview_online_abc_batch(
            session, args.tenant_id, _account_ids(args.account_ids),
            idempotency_key=args.idempotency_key,
            deployed_release_sha=args.deployed_release_sha,
        )
    if args.mode == "apply":
        return apply_online_abc_batch(
            session, args.tenant_id, _account_ids(args.account_ids),
            idempotency_key=args.idempotency_key,
            deployed_release_sha=args.deployed_release_sha,
            expected_fingerprint=args.expected_fingerprint,
            requested_by=args.requested_by,
            approved_by=args.approved_by,
            approval_ref=args.approval_ref,
        )
    if args.mode == "start":
        return start_next_online_abc_item(
            session, args.batch_id, actor=args.approved_by, approval_ref=args.approval_ref,
        )
    if args.mode == "sync":
        return sync_online_abc_batch(
            session, args.batch_id, actor=args.approved_by, approval_ref=args.approval_ref,
        )
    if args.mode == "accept":
        return accept_online_abc_observation(
            session, args.batch_id, actor=args.approved_by, approval_ref=args.approval_ref,
        )
    return online_abc_batch_status(session, args.batch_id)


def _account_ids(value: str) -> list[int]:
    return [int(item.strip()) for item in value.split(",") if item.strip()]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Guarded complete-online-ABC ten-account canary")
    parser.add_argument(
        "--mode",
        choices=("preview", "apply", "preview-full", "apply-full", "start", "sync", "accept", "status"),
        required=True,
    )
    parser.add_argument("--tenant-id", type=int, default=1)
    parser.add_argument("--account-ids", default="")
    parser.add_argument("--batch-id", default="")
    parser.add_argument("--idempotency-key", default="")
    parser.add_argument("--deployed-release-sha", default="")
    parser.add_argument("--expected-fingerprint", default="")
    parser.add_argument("--requested-by", default="")
    parser.add_argument("--approved-by", default="")
    parser.add_argument("--approval-ref", default="")
    return parser


if __name__ == "__main__":
    main()

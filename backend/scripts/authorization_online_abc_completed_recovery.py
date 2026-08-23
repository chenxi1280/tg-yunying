from __future__ import annotations

import argparse
import json

from app.database import SessionLocal
from app.services.authorization_dr.online_abc_completed_recovery import (
    apply_completed_rebase,
    apply_pre_remote_rearm,
    preview_completed_rebase,
    preview_pre_remote_rearm,
)


def main() -> int:
    args = _parser().parse_args()
    preview = args.mode.startswith("preview-")
    with SessionLocal() as session:
        function = _function(args.mode)
        common = {
            "idempotency_key": args.idempotency_key,
        }
        if not preview:
            common.update({
                "expected_fingerprint": args.expected_fingerprint,
                "requested_by": args.requested_by,
                "approved_by": args.approved_by,
                "approval_ref": args.approval_ref,
            })
        result = function(
            session, args.batch_id, args.account_id, args.case_id, **common,
        )
    print("AUTHORIZATION_ONLINE_ABC_COMPLETED_RECOVERY=" + json.dumps(result, sort_keys=True))
    return 0


def _function(mode: str):
    return {
        "preview-completed": preview_completed_rebase,
        "apply-completed": apply_completed_rebase,
        "preview-rearm": preview_pre_remote_rearm,
        "apply-rearm": apply_pre_remote_rearm,
    }[mode]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Recover completed or pre-remote online ABC checkpoints")
    parser.add_argument(
        "--mode",
        choices=("preview-completed", "apply-completed", "preview-rearm", "apply-rearm"),
        required=True,
    )
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

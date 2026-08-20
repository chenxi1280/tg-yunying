from __future__ import annotations

import argparse
import json

from app.database import SessionLocal
from app.services.authorization_dr import apply_sv_redundancy_repair, preview_sv_redundancy_repair


def main() -> None:
    args = _parser().parse_args()
    account_ids = [int(item.strip()) for item in args.account_ids.split(",") if item.strip()]
    with SessionLocal() as session:
        if args.mode == "preview":
            result = preview_sv_redundancy_repair(session, args.tenant_id, account_ids)
        else:
            result = apply_sv_redundancy_repair(
                session,
                args.tenant_id,
                account_ids,
                expected_fingerprint=args.expected_fingerprint,
                actor=args.actor,
                approval_ref=args.approval_ref,
            )
    print("AUTHORIZATION_DR_SV_REDUNDANCY=" + json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
    if result.get("failed_count", 0):
        raise SystemExit(2)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("preview", "apply"), required=True)
    parser.add_argument("--tenant-id", type=int, required=True)
    parser.add_argument("--account-ids", required=True)
    parser.add_argument("--expected-fingerprint", default="")
    parser.add_argument("--actor", default="")
    parser.add_argument("--approval-ref", default="")
    return parser


if __name__ == "__main__":
    main()

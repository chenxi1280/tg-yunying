from __future__ import annotations

import argparse
import json

from app.database import SessionLocal
from app.services.authorization_dr import (
    apply_authkey_duplicate_projection,
    preview_authkey_duplicate_projection,
)


def main() -> None:
    args = _parser().parse_args()
    account_ids = [int(value) for value in args.account_ids.split(",") if value.strip()]
    with SessionLocal() as session:
        if args.mode == "preview":
            result = preview_authkey_duplicate_projection(session, args.tenant_id, account_ids)
        else:
            result = apply_authkey_duplicate_projection(
                session,
                args.tenant_id,
                account_ids,
                expected_fingerprint=args.expected_fingerprint,
                actor=args.actor,
                approval_ref=args.approval_ref,
            )
    print("AUTHORIZATION_AUTHKEY_DUPLICATE=" + json.dumps(result, sort_keys=True), flush=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Project typed AuthKey duplicate facts to current authorizations")
    parser.add_argument("--mode", choices=("preview", "apply"), required=True)
    parser.add_argument("--tenant-id", type=int, required=True)
    parser.add_argument("--account-ids", required=True)
    parser.add_argument("--expected-fingerprint", default="")
    parser.add_argument("--actor", default="")
    parser.add_argument("--approval-ref", default="")
    return parser


if __name__ == "__main__":
    main()

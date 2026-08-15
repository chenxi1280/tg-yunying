from __future__ import annotations

import argparse
import json

from app.database import SessionLocal
from app.services.account_login.binding import backfill_phone_aliases


def main() -> int:
    parser = argparse.ArgumentParser(description="Preview or apply account phone fingerprint aliases")
    parser.add_argument("--tenant-id", type=int, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--actor", default="")
    parser.add_argument("--approval-ref", default="")
    args = parser.parse_args()
    if args.apply and (not args.actor.strip() or not args.approval_ref.strip()):
        parser.error("--apply requires --actor and --approval-ref")
    with SessionLocal() as session:
        result = backfill_phone_aliases(
            session,
            args.tenant_id,
            apply=args.apply,
            actor=args.actor,
            approval_ref=args.approval_ref,
        )
    print(json.dumps({"mode": "apply" if args.apply else "preview", **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

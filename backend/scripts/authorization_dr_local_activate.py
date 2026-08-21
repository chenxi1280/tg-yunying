from __future__ import annotations

import argparse
import json

from app.database import SessionLocal
from app.services.authorization_dr import apply_local_activate, local_activate_out, preview_local_activate


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generation-fenced SV local authorization activation")
    parser.add_argument("--mode", choices=("preview", "apply"), required=True)
    parser.add_argument("--tenant-id", type=int, required=True)
    parser.add_argument("--account-id", type=int, required=True)
    parser.add_argument("--authorization-id", type=int, required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--reason")
    parser.add_argument("--fingerprint")
    parser.add_argument("--approval-ref")
    parser.add_argument("--idempotency-key")
    return parser


def _required(args, *names: str) -> None:
    missing = [name for name in names if getattr(args, name) in (None, "")]
    if missing:
        raise SystemExit(f"missing required arguments for {args.mode}: {', '.join(missing)}")


def main() -> int:
    args = _parser().parse_args()
    with SessionLocal() as session:
        if args.mode == "preview":
            _required(args, "reason")
            case = preview_local_activate(
                session, args.tenant_id, args.account_id, args.authorization_id,
                actor=args.actor, reason=args.reason,
            )
        else:
            _required(args, "fingerprint", "approval_ref", "idempotency_key")
            case = apply_local_activate(
                session, args.tenant_id, args.account_id, args.authorization_id,
                fingerprint=args.fingerprint, actor=args.actor, approval_ref=args.approval_ref,
                idempotency_key=args.idempotency_key,
            )
        result = local_activate_out(case)
    print(json.dumps(result, default=str, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

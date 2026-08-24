from __future__ import annotations

import argparse
import json
import os

from app.database import SessionLocal
from app.services.authorization_dr.sv_two_fa_resume import (
    apply_sv_two_fa_resume,
    preview_sv_two_fa_resume,
    readback_sv_two_fa_resume,
)


def main() -> int:
    args = _parser().parse_args()
    with SessionLocal() as session:
        common = {
            "tenant_id": args.tenant_id,
            "runtime_image_sha": os.getenv("RELEASE_SHA", ""),
            "requested_by": args.requested_by,
        }
        if args.mode == "preview":
            result = preview_sv_two_fa_resume(session, args.operation_id, **common)
        elif args.mode == "apply":
            result = apply_sv_two_fa_resume(
                session, args.operation_id, actor=args.actor, approval_ref=args.approval_ref,
                idempotency_key=args.idempotency_key, expected_fingerprint=args.expected_fingerprint,
                **common,
            )
        else:
            result = readback_sv_two_fa_resume(session, args.operation_id, args.tenant_id)
    print("AUTHORIZATION_DR_SV_TWO_FA_RESUME=" + json.dumps(result, sort_keys=True), flush=True)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Resume one frozen SV B flow with tenant fixed 2FA")
    parser.add_argument("--mode", choices=("preview", "apply", "readback"), required=True)
    parser.add_argument("--tenant-id", type=int, required=True)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--requested-by", default="")
    parser.add_argument("--actor", default="")
    parser.add_argument("--approval-ref", default="")
    parser.add_argument("--idempotency-key", default="")
    parser.add_argument("--expected-fingerprint", default="")
    return parser


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import argparse
import json

from app.database import SessionLocal
from app.services.authorization_dr.sv_login_recovery import (
    apply_sv_login_recovery,
    preview_sv_login_recovery,
    readback_sv_login_recovery,
)


def main() -> None:
    args = _parser().parse_args()
    with SessionLocal() as session:
        if args.mode == "preview":
            result = preview_sv_login_recovery(
                session, args.operation_id, tenant_id=args.tenant_id,
                runtime_image_sha=args.runtime_image_sha, requested_by=args.requested_by,
            )
        elif args.mode == "apply":
            result = apply_sv_login_recovery(
                session, args.operation_id, tenant_id=args.tenant_id,
                runtime_image_sha=args.runtime_image_sha, requested_by=args.requested_by,
                actor=args.actor, approval_ref=args.approval_ref,
                idempotency_key=args.idempotency_key, expected_fingerprint=args.expected_fingerprint,
            )
        else:
            result = readback_sv_login_recovery(session, args.operation_id, args.tenant_id)
    print("AUTHORIZATION_DR_SV_LOGIN_RECOVERY=" + json.dumps(result, sort_keys=True), flush=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Recover an authorized SV login Session after DB commit failure")
    parser.add_argument("--mode", choices=("preview", "apply", "readback"), required=True)
    parser.add_argument("--tenant-id", type=int, required=True)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--runtime-image-sha", default="")
    parser.add_argument("--requested-by", default="")
    parser.add_argument("--actor", default="")
    parser.add_argument("--approval-ref", default="")
    parser.add_argument("--idempotency-key", default="")
    parser.add_argument("--expected-fingerprint", default="")
    return parser


if __name__ == "__main__":
    main()

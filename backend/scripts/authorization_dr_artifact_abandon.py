from __future__ import annotations

import argparse
import json

from app.database import SessionLocal
from app.services.authorization_dr import apply_artifact_abandon, preview_artifact_abandon


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Close an unrecoverable legacy DR artifact under approval")
    parser.add_argument("--mode", choices=("preview", "apply"), required=True)
    parser.add_argument("--tenant-id", type=int, required=True)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--expected-operation-version", type=int, required=True)
    parser.add_argument("--observed-ciphertext-digest", required=True)
    parser.add_argument("--requested-by", required=True)
    parser.add_argument("--actor", default="")
    parser.add_argument("--evidence-fingerprint", default="")
    parser.add_argument("--approval-ref", default="")
    parser.add_argument("--idempotency-key", default="")
    return parser


def main() -> int:
    args = _parser().parse_args()
    common = {
        "tenant_id": args.tenant_id,
        "expected_operation_version": args.expected_operation_version,
        "observed_ciphertext_digest": args.observed_ciphertext_digest,
        "requested_by": args.requested_by,
    }
    with SessionLocal() as session:
        if args.mode == "preview":
            result = preview_artifact_abandon(session, args.operation_id, **common)
        else:
            result = apply_artifact_abandon(
                session, args.operation_id, **common, actor=args.actor,
                evidence_fingerprint=args.evidence_fingerprint, approval_ref=args.approval_ref,
                idempotency_key=args.idempotency_key,
            )
    print(json.dumps(result, ensure_ascii=False, default=str, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

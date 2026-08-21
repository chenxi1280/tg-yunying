from __future__ import annotations

import argparse
import json

from app.database import SessionLocal
from app.services.authorization_dr import (
    apply_operation_reconcile,
    preview_operation_reconcile,
    reconcile_case_out,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Guarded authorization DR unknown reconciliation")
    parser.add_argument("--mode", choices=("preview", "apply", "readback"), required=True)
    parser.add_argument("--tenant-id", type=int, required=True)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--actor", default="codex-dr-reconcile-readback")
    parser.add_argument("--expected-operation-version", type=int)
    parser.add_argument("--blocker-code", choices=("phone_number_banned", "two_fa_invalid"))
    parser.add_argument("--event-digest")
    parser.add_argument("--source-ref")
    parser.add_argument("--runtime-image-sha")
    parser.add_argument("--node-id")
    parser.add_argument("--owner-epoch", type=int)
    parser.add_argument("--evidence-fingerprint")
    parser.add_argument("--approval-ref")
    parser.add_argument("--idempotency-key")
    return parser


def _required(args, *names: str) -> None:
    missing = [name for name in names if getattr(args, name) in (None, "")]
    if missing:
        raise SystemExit(f"missing required arguments for {args.mode}: {', '.join(missing)}")


def _preview(session, args):
    _required(
        args,
        "expected_operation_version", "blocker_code", "event_digest", "source_ref",
        "runtime_image_sha", "node_id", "owner_epoch",
    )
    evidence = {
        "kind": "historical_typed_login_failure",
        "blocker_code": args.blocker_code,
        "event_digest": args.event_digest,
        "source_ref": args.source_ref,
        "runtime_image_sha": args.runtime_image_sha,
        "node_id": args.node_id,
        "owner_epoch": args.owner_epoch,
    }
    case = preview_operation_reconcile(
        session,
        args.operation_id,
        tenant_id=args.tenant_id,
        expected_operation_version=args.expected_operation_version,
        evidence=evidence,
        actor=args.actor,
    )
    return reconcile_case_out(session, case.operation_id, args.tenant_id)


def _apply(session, args):
    _required(
        args,
        "expected_operation_version", "evidence_fingerprint", "approval_ref", "idempotency_key",
    )
    case = apply_operation_reconcile(
        session,
        args.operation_id,
        tenant_id=args.tenant_id,
        expected_operation_version=args.expected_operation_version,
        evidence_fingerprint=args.evidence_fingerprint,
        approval_ref=args.approval_ref,
        idempotency_key=args.idempotency_key,
        actor=args.actor,
    )
    return reconcile_case_out(session, case.operation_id, args.tenant_id)


def main() -> int:
    args = _parser().parse_args()
    with SessionLocal() as session:
        if args.mode == "preview":
            result = _preview(session, args)
        elif args.mode == "apply":
            result = _apply(session, args)
        else:
            result = reconcile_case_out(session, args.operation_id, args.tenant_id)
    print(json.dumps(result, default=str, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

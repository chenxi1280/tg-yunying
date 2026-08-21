from __future__ import annotations

import argparse
import json

from app.database import SessionLocal
from app.services.authorization_dr import (
    apply_operation_reconcile,
    build_pre_code_failure_evidence,
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
    parser.add_argument("--kind", choices=(
        "historical_typed_login_failure", "remote_orphan_without_bundle", "confirmed_no_remote_effect",
        "remote_unproven", "artifact_forward_recovery", "pre_code_submission_failure",
    ), default="historical_typed_login_failure")
    parser.add_argument("--blocker-code", choices=("phone_number_banned", "two_fa_invalid"))
    parser.add_argument("--event-digest")
    parser.add_argument("--source-ref")
    parser.add_argument("--runtime-image-sha")
    parser.add_argument("--node-id")
    parser.add_argument("--owner-epoch", type=int)
    parser.add_argument("--bundle-generation", type=int)
    parser.add_argument("--ciphertext-digest")
    parser.add_argument("--inventory-sequence", type=int)
    parser.add_argument("--remote-set-before-digest")
    parser.add_argument("--remote-set-after-digest")
    parser.add_argument("--new-device-count", type=int)
    parser.add_argument("--evidence-fingerprint")
    parser.add_argument("--approval-ref")
    parser.add_argument("--idempotency-key")
    return parser


def _required(args, *names: str) -> None:
    missing = [name for name in names if getattr(args, name) in (None, "")]
    if missing:
        raise SystemExit(f"missing required arguments for {args.mode}: {', '.join(missing)}")


def _preview(session, args):
    _required(args, "expected_operation_version", "event_digest", "source_ref", "runtime_image_sha")
    if args.kind == "pre_code_submission_failure":
        evidence = build_pre_code_failure_evidence(
            session,
            args.operation_id,
            tenant_id=args.tenant_id,
            event_digest=args.event_digest,
            source_ref=args.source_ref,
            runtime_image_sha=args.runtime_image_sha,
        )
    else:
        _required(args, "node_id", "owner_epoch")
        evidence = _common_evidence(args)
    if args.kind == "historical_typed_login_failure":
        _required(args, "blocker_code")
        evidence["blocker_code"] = args.blocker_code
    elif args.kind == "artifact_forward_recovery":
        _required(args, "bundle_generation", "ciphertext_digest", "inventory_sequence")
        evidence.update({
            "bundle_generation": args.bundle_generation,
            "ciphertext_digest": args.ciphertext_digest,
            "inventory_sequence": args.inventory_sequence,
        })
    elif args.kind not in {"remote_unproven", "pre_code_submission_failure"}:
        _required(args, "remote_set_before_digest", "remote_set_after_digest", "new_device_count")
        evidence.update({
            "remote_set_before_digest": args.remote_set_before_digest,
            "remote_set_after_digest": args.remote_set_after_digest,
            "new_device_count": args.new_device_count,
        })
    case = preview_operation_reconcile(
        session,
        args.operation_id,
        tenant_id=args.tenant_id,
        expected_operation_version=args.expected_operation_version,
        evidence=evidence,
        actor=args.actor,
    )
    return reconcile_case_out(session, case.operation_id, args.tenant_id)


def _common_evidence(args) -> dict:
    return {
        "kind": args.kind,
        "event_digest": args.event_digest,
        "source_ref": args.source_ref,
        "runtime_image_sha": args.runtime_image_sha,
        "node_id": args.node_id,
        "owner_epoch": args.owner_epoch,
    }


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

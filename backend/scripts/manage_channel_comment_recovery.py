from __future__ import annotations

import argparse
import json
from datetime import datetime

from app.database import SessionLocal
from app.services.task_center.channel_comment_recovery import (
    RecoveryApplyRequest,
    RecoveryPreviewRequest,
    apply_channel_comment_recovery,
    preview_channel_comment_recovery,
    readback_channel_comment_recovery,
)


def _datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _evidence(value: str) -> dict[str, str]:
    parsed = json.loads(value or "{}")
    if not isinstance(parsed, dict):
        raise ValueError("evidence-json must be an action-id to evidence-ref object")
    return {str(key): str(item) for key, item in parsed.items()}


def _preview(args) -> dict:
    request = RecoveryPreviewRequest(
        task_id=args.task_id,
        expected_deployed_sha=args.deployed_sha,
        recovery_kind=args.recovery_kind,
        operator_id=args.operator,
        approval_reference=args.approval_reference,
        previewed_at=_datetime(args.at),
        expires_at=_datetime(args.expires_at),
        exact_action_ids=tuple(filter(None, args.action_ids.split(","))),
        authoritative_no_effect_evidence=_evidence(args.evidence_json),
    )
    with SessionLocal() as session:
        manifest = preview_channel_comment_recovery(session, request)
        session.commit()
        return _manifest_payload(manifest)


def _apply(args) -> dict:
    request = RecoveryApplyRequest(
        manifest_id=args.manifest_id,
        expected_preview_hash=args.preview_hash,
        current_deployed_sha=args.deployed_sha,
        operator_id=args.operator,
        approval_reference=args.approval_reference,
        applied_at=_datetime(args.at),
    )
    with SessionLocal() as session:
        manifest = apply_channel_comment_recovery(session, request)
        session.commit()
        manifest_id = manifest.id
    with SessionLocal() as read_session:
        return readback_channel_comment_recovery(read_session, manifest_id)


def _manifest_payload(manifest) -> dict:
    return {
        "manifest_id": manifest.id,
        "task_id": manifest.task_id,
        "recovery_kind": manifest.recovery_kind,
        "preview_hash": manifest.preview_hash,
        "action_set_hash": manifest.action_set_hash,
        "exact_action_ids": list(manifest.exact_action_ids_json or []),
        "expires_at": manifest.expires_at.isoformat(),
        "manifest_state": manifest.manifest_state,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hash-locked channel comment recovery preview/apply/readback")
    parser.add_argument("--mode", choices=("preview", "apply"), required=True)
    parser.add_argument("--task-id", default="")
    parser.add_argument("--recovery-kind", default="")
    parser.add_argument("--deployed-sha", required=True)
    parser.add_argument("--operator", required=True)
    parser.add_argument("--approval-reference", required=True)
    parser.add_argument("--at", required=True, help="ISO-8601 operation timestamp")
    parser.add_argument("--expires-at", default="", help="Required for preview")
    parser.add_argument("--action-ids", default="")
    parser.add_argument("--evidence-json", default="{}")
    parser.add_argument("--manifest-id", default="")
    parser.add_argument("--preview-hash", default="")
    args = parser.parse_args(argv)
    if args.mode == "preview" and (not args.task_id or not args.recovery_kind or not args.expires_at):
        parser.error("preview requires --task-id, --recovery-kind and --expires-at")
    if args.mode == "apply" and (not args.manifest_id or not args.preview_hash):
        parser.error("apply requires --manifest-id and --preview-hash")
    result = _preview(args) if args.mode == "preview" else _apply(args)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

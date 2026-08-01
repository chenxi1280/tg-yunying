from __future__ import annotations

import argparse
from datetime import datetime
import json

from app.config import get_settings
from app.database import SessionLocal
from app.models import AiContentScopeTakeoverBatch, AuditLog
from app.services._common import _now
from app.services.task_center.ai_content_scope_takeover import (
    preview_ai_content_scope_takeover,
)
from app.services.task_center.ai_content_scope_takeover_apply import (
    apply_takeover_chunk,
    begin_takeover_apply,
)


def run_preview(
    *,
    actor: str,
    approval_ref: str,
    release_version: str,
    config_version: str,
    cutoff_at: datetime | None = None,
    supersedes_batch_id: str | None = None,
) -> dict:
    _require_approval(actor, approval_ref)
    settings = get_settings()
    with SessionLocal() as session:
        batch = preview_ai_content_scope_takeover(
            session,
            cutoff_at=cutoff_at or _now(),
            actor=actor,
            dispatcher_scope=settings.dispatcher_claim_scope,
            release_version=release_version,
            config_version=config_version,
            supersedes_batch_id=supersedes_batch_id,
        )
        _write_batch_audit(session, batch, actor, approval_ref, "preview")
        session.commit()
        return _batch_identity(batch)


def run_apply(
    *,
    batch_id: str,
    classification_hash: str,
    expected_counts: dict,
    actor: str,
    approval_ref: str,
    batch_size: int,
) -> dict:
    _require_approval(actor, approval_ref)
    with SessionLocal() as session:
        summary = begin_takeover_apply(
            session,
            batch_id,
            classification_hash=classification_hash,
            expected_counts=expected_counts,
            actor=actor,
        )
        batch = session.get(AiContentScopeTakeoverBatch, batch_id)
        _write_batch_audit(session, batch, actor, approval_ref, "apply-start")
        session.commit()
    while summary["status"] == "applying":
        with SessionLocal() as session:
            summary = apply_takeover_chunk(
                session,
                batch_id,
                classification_hash=classification_hash,
                actor=actor,
                batch_size=batch_size,
            )
            session.commit()
    return summary


def _batch_identity(batch: AiContentScopeTakeoverBatch) -> dict:
    return {
        "batch_id": batch.id,
        "classification_hash": batch.classification_hash,
        "classification_counts": dict(batch.classification_counts or {}),
        "status": batch.status,
        "cutoff_at": batch.cutoff_at,
    }


def _write_batch_audit(
    session,
    batch: AiContentScopeTakeoverBatch,
    actor: str,
    approval_ref: str,
    phase: str,
) -> None:
    session.add(AuditLog(
        tenant_id=None,
        actor=actor[:100],
        action="AI历史内容scope接管批次",
        target_type="ai_content_scope_takeover_batch",
        target_id=batch.id,
        detail=json.dumps({
            "phase": phase,
            "approval_ref": approval_ref,
            "classification_hash": batch.classification_hash,
            "classification_counts": batch.classification_counts,
        }, ensure_ascii=False, sort_keys=True),
    ))


def _require_approval(actor: str, approval_ref: str) -> None:
    if not actor.strip() or not approval_ref.strip():
        raise ValueError("takeover_actor_and_approval_required")


def _parse_cutoff(value: str) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def main() -> int:
    parser = argparse.ArgumentParser(description="AI content scope takeover.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    preview = subparsers.add_parser("preview")
    preview.add_argument("--actor", required=True)
    preview.add_argument("--approval-ref", required=True)
    preview.add_argument("--release-version", required=True)
    preview.add_argument("--config-version", required=True)
    preview.add_argument("--cutoff-at", default="")
    preview.add_argument("--supersedes-batch-id", default="")
    apply = subparsers.add_parser("apply")
    apply.add_argument("--batch-id", required=True)
    apply.add_argument("--classification-hash", required=True)
    apply.add_argument("--expected-counts-json", required=True)
    apply.add_argument("--actor", required=True)
    apply.add_argument("--approval-ref", required=True)
    apply.add_argument("--batch-size", type=int, default=100)
    args = parser.parse_args()
    if args.command == "preview":
        result = run_preview(
            actor=args.actor,
            approval_ref=args.approval_ref,
            release_version=args.release_version,
            config_version=args.config_version,
            cutoff_at=_parse_cutoff(args.cutoff_at),
            supersedes_batch_id=args.supersedes_batch_id or None,
        )
    else:
        result = run_apply(
            batch_id=args.batch_id,
            classification_hash=args.classification_hash,
            expected_counts=json.loads(args.expected_counts_json),
            actor=args.actor,
            approval_ref=args.approval_ref,
            batch_size=args.batch_size,
        )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str))
    return 0 if result["status"] == "completed" or args.command == "preview" else 1


if __name__ == "__main__":
    raise SystemExit(main())

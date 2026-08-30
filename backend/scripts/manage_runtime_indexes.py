"""Guarded concurrent index maintenance for runtime-storage optimization."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os

from app.database import SessionLocal, engine
from app.models import RuntimeCleanupAudit
from app.services.task_center.runtime_index_maintenance import (
    create_ai_memory_index,
    drop_old_ai_memory_index,
    preview_runtime_indexes,
    reindex_action_index,
    vacuum_analyze_actions,
)
from app.services.task_center.runtime_storage_maintenance import MaintenanceContext


def main() -> int:
    args = _parser().parse_args()
    context = _context(args)
    context.validate()
    before = preview_runtime_indexes(engine)
    if args.mode == "preview":
        result = before
    else:
        result = _apply(args, context)
        _audit(args, context, before, result)
    print(json.dumps(_with_context(result, context), ensure_ascii=False, sort_keys=True))
    return 0


def _apply(args, context: MaintenanceContext) -> dict:
    common = {
        "context": context,
        "expected_state_fingerprint": args.expected_state_fingerprint,
    }
    if args.mode == "create-ai-memory-index":
        return create_ai_memory_index(
            engine,
            **common,
            observed_free_bytes=args.observed_free_bytes,
            capacity_observed_at=_parse_time(args.capacity_observed_at),
        )
    if args.mode == "drop-old-ai-memory-index":
        return drop_old_ai_memory_index(engine, **common)
    if args.mode == "vacuum-actions":
        return vacuum_analyze_actions(engine, **common)
    return reindex_action_index(
        engine,
        **common,
        index_name=args.index_name,
        observed_free_bytes=args.observed_free_bytes,
        capacity_observed_at=_parse_time(args.capacity_observed_at),
    )


def _audit(args, context: MaintenanceContext, before: dict, after: dict) -> None:
    now_value = datetime.now(timezone.utc)
    with SessionLocal() as session:
        session.add(RuntimeCleanupAudit(
            cleanup_date=now_value.date(),
            status_counts={},
            deleted_counts={},
            summary={
                "cleanup_kind": "runtime_index_maintenance",
                "operation": args.mode,
                "index_name": args.index_name,
                "before_fingerprint": before["state_fingerprint"],
                "after_fingerprint": after["state_fingerprint"],
                "release_sha": context.current_release_sha,
                "actor": context.actor,
                "approval_ref": context.approval_ref,
                "observed_free_bytes": args.observed_free_bytes,
                "capacity_observed_at": args.capacity_observed_at,
            },
            created_at=now_value,
        ))
        session.commit()


def _context(args) -> MaintenanceContext:
    return MaintenanceContext(
        environment=os.getenv("APP_ENV", "").strip(),
        expected_release_sha=args.expected_release_sha,
        current_release_sha=os.getenv("RELEASE_SHA", "").strip(),
        actor=args.actor,
        approval_ref=args.approval_ref,
    )


def _with_context(result: dict, context: MaintenanceContext) -> dict:
    return {
        **result,
        "environment": context.environment,
        "release_sha": context.current_release_sha,
        "actor": context.actor,
        "approval_ref": context.approval_ref,
    }


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("runtime_index_capacity_timezone_required")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=(
        "preview",
        "create-ai-memory-index",
        "drop-old-ai-memory-index",
        "vacuum-actions",
        "reindex-action-index",
    ))
    parser.add_argument("--expected-state-fingerprint", default="")
    parser.add_argument("--index-name", default="")
    parser.add_argument("--observed-free-bytes", type=int, default=0)
    parser.add_argument("--capacity-observed-at", default="")
    parser.add_argument("--expected-release-sha", required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--approval-ref", required=True)
    return parser


if __name__ == "__main__":
    raise SystemExit(main())

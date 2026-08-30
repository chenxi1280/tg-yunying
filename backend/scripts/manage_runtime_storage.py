"""Guarded preview/apply/readback for terminal Action retention batches."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import os

from app.config import get_settings
from app.database import SessionLocal
from app.services.task_center.runtime_retention_policy import RuntimeActionRetentionPolicy
from app.services.task_center.runtime_storage_maintenance import (
    MaintenanceContext,
    apply_runtime_details_batch,
    preview_runtime_details,
    readback_runtime_details,
)


def main() -> int:
    args = _parser().parse_args()
    settings = get_settings()
    policy = RuntimeActionRetentionPolicy(
        skipped_days=settings.runtime_action_skipped_retention_days,
        success_days=settings.runtime_action_success_retention_days,
        failed_days=settings.runtime_action_failed_retention_days,
    )
    context = _context(args)
    context.validate()
    with SessionLocal() as session:
        result = _execute(session, args, policy, context)
    print(json.dumps(_with_context(result, context), ensure_ascii=False, sort_keys=True))
    return 0


def _execute(session, args, policy, context: MaintenanceContext) -> dict:
    if args.mode == "readback":
        return readback_runtime_details(
            session,
            context=context,
            expected_fingerprint=args.expected_fingerprint,
        )
    as_of = _parse_as_of(args.as_of)
    if args.mode == "preview":
        return preview_runtime_details(
            session,
            policy=policy,
            as_of=as_of,
            batch_size=args.batch_size,
        )
    result = apply_runtime_details_batch(
        session,
        context=context,
        as_of=as_of,
        expected_fingerprint=args.expected_fingerprint,
        expected_count=args.expected_count,
        policy=policy,
        batch_size=args.batch_size,
    )
    session.commit()
    return result


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


def _parse_as_of(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("runtime_storage_as_of_timezone_required")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("preview", "apply", "readback"))
    parser.add_argument("--as-of", default="")
    parser.add_argument("--batch-size", type=int, default=2000)
    parser.add_argument("--expected-fingerprint", default="")
    parser.add_argument("--expected-count", type=int, default=0)
    parser.add_argument("--expected-release-sha", required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--approval-ref", required=True)
    return parser


if __name__ == "__main__":
    raise SystemExit(main())

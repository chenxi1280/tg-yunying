from __future__ import annotations

import argparse
from datetime import date, datetime
import json

from app.database import SessionLocal

import reconcile_orphaned_source_pacing as reconciliation


def _parse_options(args) -> reconciliation.ReconcileOptions:
    deployed_sha = args.deployed_sha.strip().lower()
    reconciliation._validate_runtime_sha(deployed_sha)
    task_ids = tuple(sorted(set(args.task_id)))
    if not task_ids:
        raise ValueError("at least one task_id is required")
    return reconciliation.ReconcileOptions(
        task_ids=task_ids,
        terminal_date=date.fromisoformat(args.terminal_date),
        current_date=date.fromisoformat(args.current_date),
        rebase_anchor=datetime.fromisoformat(args.rebase_anchor),
        deployed_sha=deployed_sha,
        apply=bool(args.apply),
        expected_state_hash=args.expected_state_hash.strip().lower(),
        actor=args.actor.strip(),
        approval_ref=args.approval_ref.strip(),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Reconcile orphaned source pacing reservations and rebase "
            "current admissions."
        ),
    )
    parser.add_argument("--task-id", action="append", required=True)
    parser.add_argument("--terminal-date", required=True)
    parser.add_argument("--current-date", required=True)
    parser.add_argument("--rebase-anchor", required=True)
    parser.add_argument("--deployed-sha", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-state-hash", default="")
    parser.add_argument("--actor", default="")
    parser.add_argument("--approval-ref", default="")
    return parser


def main() -> int:
    options = _parse_options(_parser().parse_args())
    with SessionLocal() as session:
        manifest = reconciliation.build_manifest(session, options)
    state_hash = reconciliation.manifest_hash(manifest)
    print(json.dumps({
        "manifest": reconciliation._public_manifest(manifest),
        "state_hash": state_hash,
    }, ensure_ascii=False, sort_keys=True))
    if options.apply:
        result = reconciliation.apply_manifest(options, manifest)
        print(json.dumps({"apply": result}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

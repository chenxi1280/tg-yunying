from __future__ import annotations

import argparse
import json

from app.database import SessionLocal
from app.services.task_center.ledger_route_repair import (
    apply_group_ai_ledger_route_repair,
    group_ai_ledger_route_repair_hash,
    preview_group_ai_ledger_route_repair,
)


def run(args: argparse.Namespace) -> dict:
    with SessionLocal() as session:
        preview = preview_group_ai_ledger_route_repair(
            session,
            task_id=args.task_id,
            ledger_id=args.ledger_id,
        )
        manifest_hash = group_ai_ledger_route_repair_hash(preview)
        if not args.apply:
            session.rollback()
            return {"mode": "preview", "manifest": preview, "manifest_hash": manifest_hash}
        result = apply_group_ai_ledger_route_repair(
            session,
            task_id=args.task_id,
            ledger_id=args.ledger_id,
            expected_manifest_hash=args.expected_manifest_hash,
            approval_ref=args.approval_ref,
            actor=args.actor,
        )
        session.commit()
        return {"mode": "apply", **result}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Restore a group-AI task route to its current open ledger.")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--ledger-id", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-manifest-hash", default="")
    parser.add_argument("--approval-ref", default="")
    parser.add_argument("--actor", default="production-repair")
    args = parser.parse_args(argv)
    if args.apply and not args.expected_manifest_hash:
        parser.error("--expected-manifest-hash is required with --apply")
    if args.apply and not args.approval_ref:
        parser.error("--approval-ref is required with --apply")
    print(json.dumps(run(args), ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

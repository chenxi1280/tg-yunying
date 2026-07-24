from __future__ import annotations

import argparse
import json

from app.database import SessionLocal
from app.services.task_center.outbound_identity_reconcile import reconcile_outbound_identity


def reconcile(tenant_id: int | None, *, apply: bool, actor: str) -> dict[str, int]:
    with SessionLocal() as session:
        result = reconcile_outbound_identity(
            session,
            tenant_id=tenant_id,
            actor=actor,
            apply=apply,
        )
        if apply:
            session.commit()
        return {
            "bound_action_count": result.bound_action_count,
            "bound_message_task_count": result.bound_message_task_count,
            "bound_operation_task_count": result.bound_operation_task_count,
            "unresolved_action_count": result.inventory.unresolved_action_count,
            "unresolved_message_task_count": result.inventory.unresolved_message_task_count,
            "unresolved_operation_attempt_count": result.inventory.unresolved_operation_attempt_count,
            "unresolved_total": result.inventory.total,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reconcile frozen target identity for unsent Telegram outbound rows.")
    parser.add_argument("--tenant-id", type=int, default=None)
    parser.add_argument("--apply", action="store_true", help="Persist only exact same-tenant target bindings.")
    parser.add_argument("--actor", default="continuity-reconcile")
    args = parser.parse_args(argv)
    print(json.dumps(reconcile(args.tenant_id, apply=args.apply, actor=args.actor), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

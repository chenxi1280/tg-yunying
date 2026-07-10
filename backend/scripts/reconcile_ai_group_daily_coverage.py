from __future__ import annotations

import argparse
import json

from sqlalchemy import select

from app.database import SessionLocal
from app.models import Task
from app.services._common import _now
from app.services.task_center.account_scope import is_all_accounts_task, reconcile_tenant_all_account_scopes
from app.services.task_center.daily_coverage import backfill_daily_coverage_confirmations


def reconcile(tenant_id: int | None = None) -> dict[str, int]:
    totals = {
        "tenant_count": 0,
        "task_count": 0,
        "eligible_accounts": 0,
        "created_relations": 0,
        "confirmed_rows": 0,
    }
    with SessionLocal() as session:
        tenant_ids = _tenant_ids(session, tenant_id)
        for current_tenant_id in tenant_ids:
            result = reconcile_tenant_all_account_scopes(session, current_tenant_id, now=_now())
            totals["tenant_count"] += 1
            totals["task_count"] += result.task_count
            totals["eligible_accounts"] += result.eligible_accounts
            totals["created_relations"] += result.created_relations
            for task in _all_account_tasks(session, current_tenant_id):
                totals["confirmed_rows"] += backfill_daily_coverage_confirmations(
                    session,
                    task,
                    _now().date(),
                )
        session.commit()
    return totals


def _tenant_ids(session, tenant_id: int | None) -> list[int]:
    if tenant_id is not None:
        return [tenant_id]
    return [
        int(value)
        for value in session.scalars(
            select(Task.tenant_id)
            .where(Task.type == "group_ai_chat", Task.deleted_at.is_(None))
            .distinct()
        )
    ]


def _all_account_tasks(session, tenant_id: int) -> list[Task]:
    tasks = session.scalars(select(Task).where(
        Task.tenant_id == tenant_id,
        Task.type == "group_ai_chat",
        Task.deleted_at.is_(None),
        Task.status.in_(("draft", "pending", "running", "paused")),
    ))
    return [task for task in tasks if is_all_accounts_task(task)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reconcile all-account AI group task scope and daily coverage.")
    parser.add_argument("--tenant-id", type=int, default=None)
    args = parser.parse_args(argv)
    print(json.dumps(reconcile(args.tenant_id), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

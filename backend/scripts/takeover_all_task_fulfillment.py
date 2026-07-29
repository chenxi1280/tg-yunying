from __future__ import annotations

import argparse
import json

from sqlalchemy import select

from app.database import SessionLocal
from app.models import Task
from app.services.task_center.fulfillment_takeover import (
    ACTIVE_TAKEOVER_STATUSES,
    TAKEOVER_TASK_TYPES,
    takeover_task,
)


def run_takeover(*, apply: bool, tenant_id: int | None = None) -> dict:
    task_ids = _task_ids(tenant_id)
    rows: list[dict] = []
    failures: list[dict] = []
    for task_id in task_ids:
        with SessionLocal() as session:
            task = session.get(Task, task_id)
            if task is None:
                continue
            try:
                result = takeover_task(
                    session,
                    task,
                    write_audit=apply,
                )
                rows.append(result.__dict__)
                session.commit() if apply else session.rollback()
            except Exception as exc:
                session.rollback()
                failures.append(
                    {
                        "task_id": task_id,
                        "error": f"{type(exc).__name__}:{exc}",
                    }
                )
    return {
        "mode": "apply" if apply else "preview",
        "scanned": len(task_ids),
        "changed": sum(bool(row["changed"]) for row in rows),
        "tasks": rows,
        "failures": failures,
    }


def _task_ids(tenant_id: int | None) -> list[str]:
    with SessionLocal() as session:
        statement = (
            select(Task.id)
            .where(
                Task.deleted_at.is_(None),
                Task.type.in_(TAKEOVER_TASK_TYPES),
                Task.status.in_(ACTIVE_TAKEOVER_STATUSES),
            )
            .order_by(Task.created_at, Task.id)
        )
        if tenant_id is not None:
            statement = statement.where(Task.tenant_id == tenant_id)
        return list(session.scalars(statement))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Preview or apply all-task fulfillment takeover."
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--tenant-id", type=int)
    args = parser.parse_args()
    result = run_takeover(apply=args.apply, tenant_id=args.tenant_id)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 1 if result["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

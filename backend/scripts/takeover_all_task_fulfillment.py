from __future__ import annotations

import argparse
import json

from sqlalchemy import select

from app.database import SessionLocal
from app.models import Task
from app.services.task_center.fulfillment_takeover import (
    ACTIVE_TAKEOVER_STATUSES,
    TAKEOVER_TASK_TYPES,
    block_invalid_fulfillment_task,
    takeover_task,
)


STRUCTURAL_BLOCKER_PREFIXES = (
    "channel_fulfillment_",
    "comment_",
    "group_ai_chat target group not found",
    "legacy_search_click_contract_invalid:",
    "search_click_runtime_contract_invalid",
)


def run_takeover(*, apply: bool, tenant_id: int | None = None) -> dict:
    task_ids = _task_ids(tenant_id)
    rows: list[dict] = []
    blockers: list[dict] = []
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
            except ValueError as exc:
                session.rollback()
                if _is_structural_blocker(exc):
                    blockers.append(_record_structural_blocker(
                        session,
                        task_id,
                        exc,
                        apply=apply,
                    ))
                else:
                    failures.append(_failure(task_id, exc))
            except Exception as exc:
                session.rollback()
                failures.append(_failure(task_id, exc))
    return {
        "mode": "apply" if apply else "preview",
        "scanned": len(task_ids),
        "changed": sum(bool(row["changed"]) for row in rows),
        "tasks": rows,
        "blockers": blockers,
        "failures": failures,
    }


def _is_structural_blocker(exc: ValueError) -> bool:
    return str(exc).startswith(STRUCTURAL_BLOCKER_PREFIXES)


def _failure(task_id: str, exc: Exception) -> dict:
    return {
        "task_id": task_id,
        "error": f"{type(exc).__name__}:{exc}",
    }


def _record_structural_blocker(
    session,
    task_id: str,
    exc: ValueError,
    *,
    apply: bool,
) -> dict:
    detail = str(exc)
    if apply:
        task = session.get(Task, task_id)
        if task is not None:
            block_invalid_fulfillment_task(task, exc)
            session.commit()
    return {
        "task_id": task_id,
        "blocker_code": "task_contract_invalid",
        "error": detail,
        "persisted": apply,
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

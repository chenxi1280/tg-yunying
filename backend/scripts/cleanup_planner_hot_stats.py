from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys

from sqlalchemy import select


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.database import SessionLocal  # noqa: E402
from app.models import (  # noqa: E402
    Task,
    TaskRuntimeActiveBlocker,
    TaskRuntimeSummary,
)
from app.services._common import _now, audit  # noqa: E402


REMOVED_KEYS = (
    "membership_summary",
    "conversation_quality_active_blockers",
    "conversation_quality_active_blocker",
)


def main() -> None:
    args = _parse_args()
    task_ids = sorted(set(args.task_id))
    with SessionLocal() as session:
        tasks = _locked_tasks(session, task_ids, lock=args.apply)
        preview = _preview(tasks)
        print(json.dumps(preview, ensure_ascii=False, sort_keys=True))
        if not args.apply:
            return
        if preview["fingerprint"] != args.expected_fingerprint:
            raise RuntimeError("cleanup_fingerprint_mismatch")
        for task in tasks:
            _apply_task(session, task)
            audit(
                session,
                tenant_id=task.tenant_id,
                actor=args.actor,
                action="清理 Planner 热路径旧 stats",
                target_type="task",
                target_id=task.id,
                detail=args.audit_ref,
            )
        session.commit()
    with SessionLocal() as session:
        print(json.dumps({"readback": _preview(_locked_tasks(session, task_ids, lock=False))}, ensure_ascii=False, sort_keys=True))


def _parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-id", action="append", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-fingerprint", default="")
    parser.add_argument("--audit-ref", default="")
    parser.add_argument("--actor", default="codex-production-remediation")
    args = parser.parse_args()
    if args.apply and (not args.expected_fingerprint or not args.audit_ref):
        parser.error("--apply requires --expected-fingerprint and --audit-ref")
    return args


def _locked_tasks(session, task_ids: list[str], *, lock: bool) -> list[Task]:
    statement = select(Task).where(Task.id.in_(task_ids)).order_by(Task.id)
    if lock:
        statement = statement.with_for_update()
    tasks = list(session.scalars(statement))
    found = {task.id for task in tasks}
    missing = [task_id for task_id in task_ids if task_id not in found]
    if missing:
        raise RuntimeError(f"cleanup_task_missing count={len(missing)}")
    return tasks


def _preview(tasks: list[Task]) -> dict:
    rows = [_preview_row(task) for task in tasks]
    fingerprint = hashlib.sha256(
        json.dumps(rows, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "version": "planner_hot_stats_cleanup_v1",
        "task_count": len(rows),
        "fingerprint": fingerprint,
        "rows": rows,
    }


def _preview_row(task: Task) -> dict:
    stats = dict(task.stats or {})
    compact = _compact_stats(stats)
    return {
        "task_id": task.id,
        "tenant_id": task.tenant_id,
        "lifecycle_epoch": int(task.task_lifecycle_epoch or 1),
        "old_hash": _stats_hash(stats),
        "old_bytes": _stats_bytes(stats),
        "new_hash": _stats_hash(compact),
        "new_bytes": _stats_bytes(compact),
        "removed_keys": [key for key in REMOVED_KEYS if key in stats],
        "blocker_count": _legacy_blocker_count(stats),
    }


def _apply_task(session, task: Task) -> None:
    stats = dict(task.stats or {})
    _backfill_blockers(session, task, stats)
    task.stats = _compact_stats(stats)
    task.updated_at = _now()


def _compact_stats(stats: dict) -> dict:
    compact = {key: value for key, value in stats.items() if key not in REMOVED_KEYS}
    legacy = stats.get("membership_summary")
    if isinstance(legacy, dict):
        compact["membership_summary_version"] = 2
        compact["membership_summary_v2"] = {
            key: legacy.get(key)
            for key in (
                "candidate_account_count",
                "joined_account_count",
                "need_join_account_count",
                "failed_account_count",
                "unknown_after_send_count",
                "blocked_account_count",
                "estimated_membership_actions",
            )
        }
    return compact


def _backfill_blockers(session, task: Task, stats: dict) -> None:
    blockers = stats.get("conversation_quality_active_blockers")
    entries = list(blockers.items()) if isinstance(blockers, dict) else []
    singular = stats.get("conversation_quality_active_blocker")
    if singular:
        entries.append(("legacy-unscoped", singular))
    for scope_key, blocker_code in entries:
        scope_hash = _value_hash(str(scope_key))
        existing = session.scalar(select(TaskRuntimeActiveBlocker.id).where(
            TaskRuntimeActiveBlocker.tenant_id == task.tenant_id,
            TaskRuntimeActiveBlocker.task_id == task.id,
            TaskRuntimeActiveBlocker.lifecycle_epoch == int(task.task_lifecycle_epoch or 1),
            TaskRuntimeActiveBlocker.blocker_domain == "conversation_quality",
            TaskRuntimeActiveBlocker.scope_key_hash == scope_hash,
        ))
        if existing:
            continue
        session.add(TaskRuntimeActiveBlocker(
            tenant_id=task.tenant_id,
            task_id=task.id,
            lifecycle_epoch=int(task.task_lifecycle_epoch or 1),
            blocker_domain="conversation_quality",
            scope_key_hash=scope_hash,
            blocker_code=str(blocker_code)[:80],
            source_type="legacy_stats",
            source_id_hash=scope_hash,
        ))
    if entries:
        _update_runtime_summary(session, task)


def _update_runtime_summary(session, task: Task) -> None:
    summary = session.scalar(select(TaskRuntimeSummary).where(
        TaskRuntimeSummary.tenant_id == task.tenant_id,
        TaskRuntimeSummary.task_id == task.id,
    ).with_for_update())
    if summary is None:
        summary = TaskRuntimeSummary(
            tenant_id=task.tenant_id,
            task_id=task.id,
            task_status=task.status,
            lifecycle_epoch=int(task.task_lifecycle_epoch or 1),
        )
        session.add(summary)
    session.flush()
    blockers = list(session.scalars(select(TaskRuntimeActiveBlocker).where(
        TaskRuntimeActiveBlocker.tenant_id == task.tenant_id,
        TaskRuntimeActiveBlocker.task_id == task.id,
        TaskRuntimeActiveBlocker.lifecycle_epoch == int(task.task_lifecycle_epoch or 1),
    ).order_by(TaskRuntimeActiveBlocker.opened_at, TaskRuntimeActiveBlocker.id)))
    payload = dict(summary.summary or {})
    payload["runtime_blocker_summary_v2"] = {
        "active_count": len(blockers),
        "code_counts": dict(Counter(row.blocker_code for row in blockers)),
        "revision": int(summary.blocker_revision or 0) + 1,
        "samples": [row.scope_key_hash for row in blockers[:10]],
        "migration_source": "legacy_stats_cleanup",
    }
    summary.summary = payload
    summary.blocker_revision = int(summary.blocker_revision or 0) + 1


def _legacy_blocker_count(stats: dict) -> int:
    blockers = stats.get("conversation_quality_active_blockers")
    count = len(blockers) if isinstance(blockers, dict) else 0
    return count + int(bool(stats.get("conversation_quality_active_blocker")))


def _stats_hash(stats: dict) -> str:
    return hashlib.sha256(
        json.dumps(stats, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _stats_bytes(stats: dict) -> int:
    return len(json.dumps(stats, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8"))


def _value_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


if __name__ == "__main__":
    main()

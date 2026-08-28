from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, dataclass

from sqlalchemy import func, select

from app.database import SessionLocal
from app.models import Action, ExecutionAttempt, OperationTarget, Task
from app.schemas.task_center import TaskSettingsUpdate
from app.services._common import audit
from app.services.task_center.admission_epoch_recovery import (
    ADMISSION_ACTION_TYPES,
    replan_stale_admission_actions,
)
from app.services.task_center.runtime_state_hash import canonical_state_hash
from app.services.task_center.service import update_task_settings
from app.services.task_center.targets import group_from_reference


@dataclass(frozen=True)
class TaskRecoverySnapshot:
    task_id: str
    task_name: str
    status: str
    lifecycle_epoch: int
    config_revision: int
    target_operation_target_id: int
    target_group_id: int
    stale_zero_attempt_count: int
    stale_with_attempt_count: int


def preview(
    task_ids: tuple[str, ...],
    *,
    target_task_id: str,
    target_username: str,
    deployed_sha: str,
) -> dict:
    _validate_runtime_sha(deployed_sha)
    with SessionLocal() as session:
        tasks = _tasks(session, task_ids, lock=False)
        target_change = _target_change_preview(
            session,
            tasks,
            target_task_id=target_task_id,
            target_username=target_username,
        )
        snapshots = [_task_snapshot(session, task) for task in tasks]
        body = _preview_body(snapshots, target_change, deployed_sha)
        return {**body, "fingerprint": canonical_state_hash(body)}


def apply(
    task_ids: tuple[str, ...],
    *,
    target_task_id: str,
    target_username: str,
    expected_fingerprint: str,
    actor: str,
    approval_reference: str,
    deployed_sha: str,
) -> dict:
    _require_apply_fields(expected_fingerprint, actor, approval_reference)
    _validate_runtime_sha(deployed_sha)
    with SessionLocal() as session:
        tasks = _tasks(session, task_ids, lock=True)
        target_change = _target_change_preview(
            session,
            tasks,
            target_task_id=target_task_id,
            target_username=target_username,
        )
        snapshots = [_task_snapshot(session, task) for task in tasks]
        body = _preview_body(snapshots, target_change, deployed_sha)
        _require_fingerprint(body, expected_fingerprint)
        expected_snapshots = {row.task_id: row for row in snapshots}
        session.rollback()
    target_result = _apply_target_change(
        target_change,
        actor=actor,
        approval_reference=approval_reference,
    )
    epoch_result = _apply_epoch_recovery(
        task_ids,
        skip_task_id=target_task_id,
        actor=actor,
        approval_reference=approval_reference,
        expected_snapshots=expected_snapshots,
    )
    return {
        "mode": "apply",
        "target_change": target_result,
        "epoch_recovery": epoch_result,
    }


def _tasks(session, task_ids: tuple[str, ...], *, lock: bool) -> list[Task]:
    if not task_ids or len(set(task_ids)) != len(task_ids):
        raise ValueError("task_ids_must_be_unique_and_non_empty")
    statement = select(Task).where(
        Task.id.in_(task_ids),
        Task.type == "group_ai_chat",
        Task.deleted_at.is_(None),
    ).order_by(Task.id)
    if lock:
        statement = statement.with_for_update()
    tasks = list(session.scalars(statement))
    if {task.id for task in tasks} != set(task_ids):
        raise ValueError("exact_group_ai_task_scope_mismatch")
    return tasks


def _task_snapshot(session, task: Task) -> TaskRecoverySnapshot:
    zero_attempt, with_attempt = _stale_counts(session, task)
    config = dict(task.type_config or {})
    return TaskRecoverySnapshot(
        task_id=task.id,
        task_name=task.name,
        status=task.status,
        lifecycle_epoch=int(task.task_lifecycle_epoch or 1),
        config_revision=int(task.config_revision or 1),
        target_operation_target_id=int(config.get("target_operation_target_id") or 0),
        target_group_id=int(config.get("target_group_id") or 0),
        stale_zero_attempt_count=zero_attempt,
        stale_with_attempt_count=with_attempt,
    )


def _stale_counts(session, task: Task) -> tuple[int, int]:
    rows = list(session.scalars(select(Action).where(
        Action.task_id == task.id,
        Action.action_type.in_(ADMISSION_ACTION_TYPES),
        Action.status == "pending",
        Action.task_lifecycle_epoch != int(task.task_lifecycle_epoch or 1),
    )))
    attempted = set(session.scalars(select(ExecutionAttempt.action_id).where(
        ExecutionAttempt.action_id.in_([row.id for row in rows]),
    ))) if rows else set()
    return sum(row.id not in attempted for row in rows), sum(row.id in attempted for row in rows)


def _target_change_preview(
    session,
    tasks: list[Task],
    *,
    target_task_id: str,
    target_username: str,
) -> dict:
    if not target_task_id and not target_username:
        return {}
    if not target_task_id or not target_username:
        raise ValueError("target_task_id_and_username_must_be_paired")
    task = next((row for row in tasks if row.id == target_task_id), None)
    if task is None:
        raise ValueError("target_task_not_in_exact_scope")
    target = _unique_username_target(session, task.tenant_id, target_username)
    group = group_from_reference(
        session,
        task.tenant_id,
        operation_target_id=target.id,
        require_authorized=False,
    )
    if group is None:
        raise ValueError("target_username_group_unresolved")
    config = dict(task.type_config or {})
    return {
        "task_id": task.id,
        "target_username": target_username.lstrip("@"),
        "old_operation_target_id": int(config.get("target_operation_target_id") or 0),
        "old_group_id": int(config.get("target_group_id") or 0),
        "new_operation_target_id": int(target.id),
        "new_group_id": int(group.id),
        "expected_lifecycle_epoch": int(task.task_lifecycle_epoch or 1),
        "expected_config_revision": int(task.config_revision or 1),
    }


def _unique_username_target(session, tenant_id: int, username: str) -> OperationTarget:
    normalized = username.strip().lstrip("@").lower()
    rows = list(session.scalars(select(OperationTarget).where(
        OperationTarget.tenant_id == tenant_id,
        OperationTarget.target_type == "group",
        func.lower(OperationTarget.username) == normalized,
    )))
    if len(rows) != 1:
        raise ValueError(f"target_username_match_count:{len(rows)}")
    return rows[0]


def _preview_body(
    snapshots: list[TaskRecoverySnapshot],
    target_change: dict,
    deployed_sha: str,
) -> dict:
    return {
        "mode": "preview",
        "deployed_sha": deployed_sha.lower(),
        "tasks": [asdict(snapshot) for snapshot in snapshots],
        "target_change": target_change,
        "total_stale_zero_attempt_count": sum(
            snapshot.stale_zero_attempt_count for snapshot in snapshots
        ),
        "total_stale_with_attempt_count": sum(
            snapshot.stale_with_attempt_count for snapshot in snapshots
        ),
    }


def _require_apply_fields(
    fingerprint: str,
    actor: str,
    approval_reference: str,
) -> None:
    if not fingerprint or not actor or not approval_reference:
        raise ValueError("apply_requires_fingerprint_actor_and_approval_reference")


def _require_fingerprint(body: dict, expected: str) -> None:
    observed = canonical_state_hash(body)
    if observed != expected:
        raise RuntimeError(
            f"preview_fingerprint_drift:expected={expected}:observed={observed}"
        )


def _validate_runtime_sha(deployed_sha: str) -> None:
    runtime_sha = str(
        os.getenv("RELEASE_SHA") or os.getenv("GIT_SHA") or ""
    ).lower()
    if len(deployed_sha) != 40 or runtime_sha != deployed_sha.lower():
        raise RuntimeError("ai_group_membership_recovery_deployed_sha_mismatch")


def _apply_target_change(
    change: dict,
    *,
    actor: str,
    approval_reference: str,
) -> dict:
    if not change:
        return {}
    with SessionLocal() as session:
        locked = session.scalar(
            select(Task)
            .where(Task.id == change["task_id"])
            .with_for_update()
        )
        _require_target_change_state(locked, change)
        task = update_task_settings(
            session,
            _task_tenant_id(session, change["task_id"]),
            change["task_id"],
            TaskSettingsUpdate(
                target_operation_target_id=change["new_operation_target_id"],
                target_group_id=change["new_group_id"],
            ),
            actor,
        )
        audit(
            session,
            tenant_id=task.tenant_id,
            actor=actor,
            action="受保护修复AI活群目标身份",
            target_type="task",
            target_id=task.id,
            detail=(
                f"approval={approval_reference};"
                f"target={change['new_operation_target_id']};"
                f"group={change['new_group_id']}"
            ),
        )
        session.commit()
        return _target_readback(task)


def _require_target_change_state(task: Task | None, change: dict) -> None:
    if task is None:
        raise ValueError("task_not_found")
    config = dict(task.type_config or {})
    observed = (
        int(task.task_lifecycle_epoch or 1),
        int(task.config_revision or 1),
        int(config.get("target_operation_target_id") or 0),
        int(config.get("target_group_id") or 0),
    )
    expected = (
        change["expected_lifecycle_epoch"],
        change["expected_config_revision"],
        change["old_operation_target_id"],
        change["old_group_id"],
    )
    if observed != expected:
        raise RuntimeError("target_change_state_drift")


def _task_tenant_id(session, task_id: str) -> int:
    task = session.get(Task, task_id)
    if task is None:
        raise ValueError("task_not_found")
    return int(task.tenant_id)


def _target_readback(task: Task) -> dict:
    config = dict(task.type_config or {})
    return {
        "task_id": task.id,
        "lifecycle_epoch": int(task.task_lifecycle_epoch or 1),
        "config_revision": int(task.config_revision or 1),
        "target_operation_target_id": int(config.get("target_operation_target_id") or 0),
        "target_group_id": int(config.get("target_group_id") or 0),
    }


def _apply_epoch_recovery(
    task_ids: tuple[str, ...],
    *,
    skip_task_id: str,
    actor: str,
    approval_reference: str,
    expected_snapshots: dict[str, TaskRecoverySnapshot],
) -> dict[str, int]:
    with SessionLocal() as session:
        tasks = _tasks(session, task_ids, lock=True)
        _require_epoch_snapshots(session, tasks, expected_snapshots, skip_task_id)
        counts = {
            task.id: replan_stale_admission_actions(session, task=task)
            for task in tasks
            if task.id != skip_task_id
        }
        for task in tasks:
            if counts.get(task.id, 0):
                audit(
                    session,
                    tenant_id=task.tenant_id,
                    actor=actor,
                    action="批准AI活群准入epoch恢复",
                    target_type="task",
                    target_id=task.id,
                    detail=f"approval={approval_reference};count={counts[task.id]}",
                )
        session.commit()
        return counts


def _require_epoch_snapshots(
    session,
    tasks: list[Task],
    expected: dict[str, TaskRecoverySnapshot],
    skip_task_id: str,
) -> None:
    for task in tasks:
        if task.id == skip_task_id:
            continue
        if _task_snapshot(session, task) != expected[task.id]:
            raise RuntimeError(f"epoch_recovery_state_drift:{task.id}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-id", action="append", required=True)
    parser.add_argument("--target-task-id", default="")
    parser.add_argument("--target-username", default="")
    parser.add_argument("--deployed-sha", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-fingerprint", default="")
    parser.add_argument("--actor", default="")
    parser.add_argument("--approval-reference", default="")
    args = parser.parse_args(argv)
    task_ids = tuple(args.task_id)
    result = apply(
        task_ids,
        target_task_id=args.target_task_id,
        target_username=args.target_username,
        expected_fingerprint=args.expected_fingerprint,
        actor=args.actor,
        approval_reference=args.approval_reference,
        deployed_sha=args.deployed_sha,
    ) if args.apply else preview(
        task_ids,
        target_task_id=args.target_task_id,
        target_username=args.target_username,
        deployed_sha=args.deployed_sha,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Preview or enable AI Content Route V2 for running AI group tasks."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import Task, TgGroup
from app.services._common import audit
from app.services.task_center.config_normalization import validated_type_config
from app.services.task_center.daily_group_target import ensure_task_group_daily_target
from app.services.task_center.task_ai_content_activation import (
    activate_task_ai_content_config,
    validate_task_ai_content_config,
)


def _candidate_config(task: Task) -> dict:
    config = dict(task.type_config or {})
    config["ai_content_route_v2_enabled"] = True
    config["ai_two_stage_enabled"] = True
    config.pop("ai_provider_id", None)
    return validated_type_config(task.type, config)


def _validate_candidate(
    session: Session,
    task: Task,
    config: dict,
    *,
    increment_revision: bool,
) -> None:
    original_config = task.type_config
    original_revision = task.config_revision
    try:
        with session.no_autoflush:
            task.type_config = config
            task.config_revision = int(original_revision or 0) + int(increment_revision)
            validate_task_ai_content_config(session, task)
    finally:
        task.type_config = original_config
        task.config_revision = original_revision


def _print_preview(task: Task, config: dict, *, increment_revision: bool) -> None:
    old = dict(task.type_config or {})
    next_revision = int(task.config_revision or 0) + int(increment_revision)
    print(f"\n- [{task.name}] (ID: {task.id})")
    print(f"  Config revision: {task.config_revision} -> {next_revision}")
    print(f"  Route V2: {old.get('ai_content_route_v2_enabled')} -> true")
    print(f"  Two-stage: {old.get('ai_two_stage_enabled')} -> true")
    print(f"  Policy ID: {config.get('ai_content_policy_version_id')}")
    print(f"  Allowed routes: {config.get('ai_content_allowed_routes')}")


def _apply_candidate(
    session: Session,
    task: Task,
    config: dict,
    today,
    *,
    increment_revision: bool,
) -> None:
    previous_revision = int(task.config_revision or 0)
    task.type_config = config
    task.config_revision = previous_revision + int(increment_revision)
    task.updated_at = datetime.now(timezone.utc)
    activate_task_ai_content_config(session, task)
    group_id = config.get("target_group_id")
    group = session.get(TgGroup, group_id) if group_id else None
    if group is not None:
        refreshed = ensure_task_group_daily_target(session, task, group, today)
        print(f"  [APPLIED] Refreshed Ledger: effective={refreshed.effective_message_target}")
    audit(
        session,
        tenant_id=task.tenant_id,
        actor="production-ai-group-target-tuning",
        action="enable_ai_content_route_v2",
        target_type="task",
        target_id=task.id,
        detail=json.dumps({
            "previous_config_revision": previous_revision,
            "config_revision": task.config_revision,
            "policy_version_id": config.get("ai_content_policy_version_id"),
            "allowed_routes": config.get("ai_content_allowed_routes") or [],
        }, ensure_ascii=False, sort_keys=True),
    )


def _load_tasks(
    session: Session,
    task_id: str | None,
    *,
    for_update: bool,
) -> list[Task]:
    query = select(Task).where(
        Task.type == "group_ai_chat",
        Task.status == "running",
        Task.deleted_at.is_(None),
    )
    if task_id:
        query = query.where(Task.id == task_id)
    if for_update:
        query = query.with_for_update()
    tasks = list(session.scalars(query).all())
    if task_id and not tasks:
        raise ValueError("running_group_ai_task_not_found")
    return tasks


def run(*, apply: bool, task_id: str | None) -> None:
    with SessionLocal() as session:
        tasks = _load_tasks(session, task_id, for_update=apply)
        mode = "APPLY" if apply else "PREVIEW"
        print(f"=== ENABLE AI ROUTE V2 TOOL (Mode: {mode}) ===")
        print(f"Found {len(tasks)} running AI group tasks.")
        candidates = {task.id: _candidate_config(task) for task in tasks}
        revisions = {
            task.id: candidates[task.id] != dict(task.type_config or {})
            for task in tasks
        }
        for task in tasks:
            _validate_candidate(
                session, task, candidates[task.id],
                increment_revision=revisions[task.id],
            )
            _print_preview(
                task, candidates[task.id],
                increment_revision=revisions[task.id],
            )
        if not apply:
            print("\n>>> Preview validation passed. Run with --apply to commit changes.")
            return
        today = datetime.now(timezone.utc).date()
        for task in tasks:
            _apply_candidate(
                session, task, candidates[task.id], today,
                increment_revision=revisions[task.id],
            )
        session.commit()
        print("\n>>> All validated tasks switched to AI Route V2.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Enable AI Content Route V2")
    parser.add_argument("--apply", action="store_true", help="Apply validated changes")
    parser.add_argument("--task-id", type=str, default=None, help="Specific running task ID")
    args = parser.parse_args()
    run(apply=args.apply, task_id=args.task_id)


if __name__ == "__main__":
    main()

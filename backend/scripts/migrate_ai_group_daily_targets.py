from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import (
    Action,
    AiAccountVoiceProfile,
    AuditLog,
    Task,
    TaskAccountDailyCoverage,
    TaskGroupDailyTarget,
    TaskMembershipAdmissionItem,
)
from app.services._common import _now
from app.services.task_center.config_normalization import normalize_ai_daily_target
from app.services.task_center.daily_group_target import ensure_task_group_daily_target
from app.services.task_center.targets import group_from_reference

OPEN_ACTION_STATUSES = ("pending", "retryable_failed")
LEGACY_HARD_FIELDS = (
    "hard_hourly_target",
    "hard_hourly_bucket",
    "hard_hourly_goal_at_plan",
    "hard_hourly_deficit_at_plan",
)
LEGACY_CHECK_IN_SOURCES = ("direct_check_in", "check_in_fallback")


@dataclass
class MigrationSummary:
    tasks_scanned: int = 0
    tasks_changed: int = 0
    targets_created: int = 0
    hard_actions_rebound: int = 0
    actions_skipped: int = 0
    check_ins_converted: int = 0
    coverage_rows_normalized: int = 0
    task_errors: list[str] = field(default_factory=list)


def migrate(*, apply: bool, tenant_id: int | None = None) -> dict:
    summary = MigrationSummary()
    with SessionLocal() as session:
        tasks = list(session.scalars(_task_query(tenant_id)))
        for task in tasks:
            summary.tasks_scanned += 1
            try:
                with session.begin_nested():
                    changed = _migrate_task(session, task, summary, apply=apply)
                    summary.tasks_changed += int(changed)
            except Exception as exc:
                summary.task_errors.append(f"{task.id}:{type(exc).__name__}:{exc}")
        if apply:
            session.commit()
        else:
            session.rollback()
    return summary.__dict__


def normalized_task_config(config: dict, frozen_account_count: int) -> dict:
    return normalize_ai_daily_target(config, frozen_account_count=frozen_account_count)


def migrated_open_payload(payload: dict, *, coverage_bound: bool) -> tuple[dict, str]:
    updated = dict(payload or {})
    was_hard = bool(updated.get("hard_hourly_target"))
    if was_hard and not coverage_bound:
        return updated, "skip_unbound_hard_target"
    for legacy_field in LEGACY_HARD_FIELDS:
        updated.pop(legacy_field, None)
    if was_hard:
        return updated, "rebound_daily_target"
    return updated, "unchanged"


def _task_query(tenant_id: int | None):
    query = select(Task).where(
        Task.type == "group_ai_chat",
        Task.deleted_at.is_(None),
        Task.status.in_(("draft", "pending", "running", "paused")),
    )
    return query.where(Task.tenant_id == tenant_id) if tenant_id is not None else query


def _migrate_task(
    session: Session,
    task: Task,
    summary: MigrationSummary,
    *,
    apply: bool,
) -> bool:
    frozen = _frozen_account_count(session, task)
    normalized = normalized_task_config(task.type_config or {}, frozen)
    changed = normalized != (task.type_config or {})
    group = _task_group(session, task, normalized)
    if apply and changed:
        task.type_config = normalized
        task.hard_hourly_next_check_at = None
    if group is not None:
        target_date = _now().date()
        existing = session.scalar(select(TaskGroupDailyTarget.id).where(
            TaskGroupDailyTarget.task_id == task.id,
            TaskGroupDailyTarget.group_id == group.id,
            TaskGroupDailyTarget.target_date == target_date,
        ).limit(1))
        summary.targets_created += int(existing is None)
        changed = changed or existing is None
        if apply:
            ensure_task_group_daily_target(session, task, group, target_date)
    normalized_rows = _normalize_coverage_rows(session, task, apply=apply)
    summary.coverage_rows_normalized += normalized_rows
    changed = changed or normalized_rows > 0
    for action in _open_send_actions(session, task):
        changed = _migrate_action(session, task, action, summary, apply=apply) or changed
    if apply and changed:
        _audit_task(session, task, summary)
    return changed


def _migrate_action(
    session: Session,
    task: Task,
    action: Action,
    summary: MigrationSummary,
    *,
    apply: bool,
) -> bool:
    payload = action.payload or {}
    coverage_bound = bool(payload.get("coverage_ledger_id"))
    updated, decision = migrated_open_payload(payload, coverage_bound=coverage_bound)
    if decision == "skip_unbound_hard_target":
        return _skip_action(action, summary, "legacy_hard_target_without_coverage", apply)
    changed = updated != payload
    if decision == "rebound_daily_target":
        summary.hard_actions_rebound += 1
    source = str(updated.get("content_source") or updated.get("ai_generation_source") or "")
    if source in LEGACY_CHECK_IN_SOURCES:
        return _migrate_check_in(session, task, action, updated, summary, apply=apply) or changed
    if apply and changed:
        action.payload = updated
    return changed


def _migrate_check_in(
    session: Session,
    task: Task,
    action: Action,
    payload: dict,
    summary: MigrationSummary,
    *,
    apply: bool,
) -> bool:
    coverage_id = str(payload.get("coverage_ledger_id") or "")
    coverage = session.get(TaskAccountDailyCoverage, coverage_id) if coverage_id else None
    if (
        not coverage
        or coverage.task_id != task.id
        or coverage.account_id != action.account_id
        or not action.account_id
    ):
        return _skip_action(action, summary, "superseded_by_daily_group_target", apply)
    if _has_active_mask(session, task.tenant_id, action.account_id):
        return _skip_action(action, summary, "superseded_by_masked_coverage_generation", apply)
    updated = {
        **payload,
        "message": "签到",
        "message_text": "签到",
        "group_id": coverage.group_id,
        "content_source": "mask_missing_check_in",
        "ai_generation_source": "mask_missing_check_in",
        "mask_status": "missing",
        "fallback_obligation_key": (
            f"{task.id}:{coverage.group_id}:{action.account_id}:"
            f"{coverage.coverage_date.isoformat()}:mask_missing_check_in"
        ),
    }
    if apply:
        action.payload = updated
    summary.check_ins_converted += 1
    return True


def _skip_action(action: Action, summary: MigrationSummary, reason: str, apply: bool) -> bool:
    if apply:
        action.status = "skipped"
        action.result = {**(action.result or {}), "skip_reason": reason}
    summary.actions_skipped += 1
    return True


def _frozen_account_count(session: Session, task: Task) -> int:
    return int(session.scalar(select(func.count(TaskMembershipAdmissionItem.id)).where(
        TaskMembershipAdmissionItem.tenant_id == task.tenant_id,
        TaskMembershipAdmissionItem.task_id == task.id,
    )) or 0)


def _task_group(session: Session, task: Task, config: dict):
    return group_from_reference(
        session,
        task.tenant_id,
        group_id=int(config.get("target_group_id") or 0) or None,
        operation_target_id=int(config.get("target_operation_target_id") or 0) or None,
        require_authorized=False,
    )


def _open_send_actions(session: Session, task: Task) -> list[Action]:
    return list(session.scalars(select(Action).where(
        Action.tenant_id == task.tenant_id,
        Action.task_id == task.id,
        Action.action_type == "send_message",
        Action.status.in_(OPEN_ACTION_STATUSES),
    )))


def _normalize_coverage_rows(session: Session, task: Task, *, apply: bool) -> int:
    count = int(session.scalar(select(func.count(TaskAccountDailyCoverage.id)).where(
        TaskAccountDailyCoverage.task_id == task.id,
        TaskAccountDailyCoverage.target_count != 1,
    )) or 0)
    if apply and count:
        session.execute(
            update(TaskAccountDailyCoverage)
            .where(
                TaskAccountDailyCoverage.task_id == task.id,
                TaskAccountDailyCoverage.target_count != 1,
            )
            .values(target_count=1)
        )
    return count


def _has_active_mask(session: Session, tenant_id: int, account_id: int) -> bool:
    return bool(session.scalar(select(AiAccountVoiceProfile.id).where(
        AiAccountVoiceProfile.tenant_id == tenant_id,
        AiAccountVoiceProfile.account_id == account_id,
        AiAccountVoiceProfile.status == "active",
        AiAccountVoiceProfile.quality_status == "active",
        AiAccountVoiceProfile.short_prompt_summary != "",
    ).limit(1)))


def _audit_task(session: Session, task: Task, summary: MigrationSummary) -> None:
    session.add(AuditLog(
        tenant_id=task.tenant_id,
        actor="system:ai_daily_target_migration",
        action="migrate_ai_group_daily_target",
        target_type="task",
        target_id=task.id,
        detail=json.dumps(summary.__dict__, ensure_ascii=False, sort_keys=True),
    ))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Migrate AI group tasks to daily group targets.")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--tenant-id", type=int)
    args = parser.parse_args(argv)
    print(json.dumps(migrate(apply=args.apply, tenant_id=args.tenant_id), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

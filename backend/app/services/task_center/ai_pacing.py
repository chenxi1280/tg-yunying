from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Action,
    Task,
    TaskAccountDailyCoverage,
    TaskDayLedger,
    TaskGroupDailyMessageSlot,
    TaskGroupDailyTarget,
    TgGroup,
)

from .source_pacing import SourcePacingSlot, wall_datetime
from .source_owner_cursor import attach_owner_history, pacing_source_key_hash


ACTIVE_QUANTITY_ACTION_STATUSES = (
    "pending",
    "claiming",
    "executing",
    "success",
    "unknown_after_send",
)


@dataclass(frozen=True)
class AiPacingAssignment:
    item_index: int
    owner: TaskGroupDailyMessageSlot
    source_slot: SourcePacingSlot


def assign_ai_pacing_slots(
    session: Session,
    task: Task,
    *,
    daily_group_target_id: str,
    effective_plan_total: int,
    coverage_by_account: dict[int, TaskAccountDailyCoverage],
    item_account_ids: list[int],
) -> list[AiPacingAssignment]:
    target = session.get(TaskGroupDailyTarget, daily_group_target_id)
    if target is None or not target.task_day_ledger_id:
        return []
    ledger = session.get(TaskDayLedger, target.task_day_ledger_id)
    if ledger is None:
        return []
    expected_ids = _coverage_expectations(coverage_by_account, item_account_ids)
    available = _available_quantity_slots(
        session,
        task,
        ledger.id,
        expected_coverage_ids=expected_ids,
    )
    owners = _align_quantity_slots(available, coverage_by_account, item_account_ids)
    plan_total = max(
        effective_plan_total,
        max((row.pacing_plan_total or row.slot_ordinal for row in owners), default=0),
    )
    assignments = _assignments_for_owners(
        session,
        task,
        target=target,
        ledger=ledger,
        owners=owners,
        plan_total=plan_total,
    )
    enriched = attach_owner_history(
        session,
        task,
        [item.source_slot for item in assignments],
        owner_model=TaskGroupDailyMessageSlot,
        config=task.pacing_config or {},
        seed_id=f"ai:{task.id}",
    )
    return [
        AiPacingAssignment(item.item_index, item.owner, enriched[index])
        for index, item in enumerate(assignments)
    ]


def _assignments_for_owners(
    session: Session,
    task: Task,
    *,
    target: TaskGroupDailyTarget,
    ledger: TaskDayLedger,
    owners: list[TaskGroupDailyMessageSlot],
    plan_total: int,
) -> list[AiPacingAssignment]:
    group = session.get(TgGroup, target.group_id)
    if group is None:
        return []
    source_hash = pacing_source_key_hash(group.tg_peer_id)
    return [
        AiPacingAssignment(
            index,
            owner,
            _source_slot(
                task,
                ledger,
                owner,
                plan_total=plan_total,
                source_hash=source_hash,
            ),
        )
        for index, owner in enumerate(owners)
    ]


def _available_quantity_slots(
    session: Session,
    task: Task,
    ledger_id: str,
    *,
    expected_coverage_ids: list[str],
) -> list[TaskGroupDailyMessageSlot]:
    bound_action = select(Action.id).where(
        Action.primary_quantity_slot_id == TaskGroupDailyMessageSlot.id,
        Action.status.in_(ACTIVE_QUANTITY_ACTION_STATUSES),
    ).exists()
    statement = select(TaskGroupDailyMessageSlot).where(
        TaskGroupDailyMessageSlot.task_id == task.id,
        TaskGroupDailyMessageSlot.task_day_ledger_id == ledger_id,
        TaskGroupDailyMessageSlot.state == "open",
        ~bound_action,
    ).order_by(TaskGroupDailyMessageSlot.slot_ordinal.asc())
    specific_ids = sorted({coverage_id for coverage_id in expected_coverage_ids if coverage_id})
    rows: list[TaskGroupDailyMessageSlot] = []
    if specific_ids:
        rows.extend(session.scalars(_lock_slots(
            session,
            statement.where(TaskGroupDailyMessageSlot.task_account_daily_coverage_id.in_(specific_ids)),
        )))
    unassigned_count = expected_coverage_ids.count("")
    if unassigned_count:
        rows.extend(session.scalars(_lock_slots(
            session,
            statement.where(TaskGroupDailyMessageSlot.task_account_daily_coverage_id.is_(None)).limit(unassigned_count),
        )))
    return sorted(rows, key=lambda row: row.slot_ordinal)


def _lock_slots(session: Session, statement):
    if session.get_bind().dialect.name == "sqlite":
        return statement
    return statement.with_for_update(of=TaskGroupDailyMessageSlot)


def _align_quantity_slots(
    available: list[TaskGroupDailyMessageSlot],
    coverage_by_account: dict[int, TaskAccountDailyCoverage],
    item_account_ids: list[int],
) -> list[TaskGroupDailyMessageSlot]:
    expected_ids = _coverage_expectations(coverage_by_account, item_account_ids)
    coverage_slots: dict[str, TaskGroupDailyMessageSlot] = {}
    unassigned: list[TaskGroupDailyMessageSlot] = []
    for row in available:
        coverage_id = str(row.task_account_daily_coverage_id or "")
        if coverage_id:
            coverage_slots.setdefault(coverage_id, row)
        else:
            unassigned.append(row)
    unassigned_iter = iter(unassigned)
    selected: list[TaskGroupDailyMessageSlot] = []
    for expected_id in expected_ids:
        matched = coverage_slots.pop(expected_id, None) if expected_id else next(unassigned_iter, None)
        if matched is None:
            break
        selected.append(matched)
    return selected


def _coverage_expectations(
    coverage_by_account: dict[int, TaskAccountDailyCoverage],
    item_account_ids: list[int],
) -> list[str]:
    assigned: set[str] = set()
    expected: list[str] = []
    for account_id in item_account_ids:
        coverage_id = _incomplete_coverage_id(coverage_by_account.get(account_id))
        current = coverage_id if coverage_id not in assigned else ""
        expected.append(current)
        if current:
            assigned.add(current)
    return expected


def _incomplete_coverage_id(coverage: TaskAccountDailyCoverage | None) -> str:
    if coverage is None:
        return ""
    target = max(1, int(coverage.target_count or 1))
    if int(coverage.confirmed_count or 0) >= target:
        return ""
    return str(coverage.id)


def _source_slot(
    task: Task,
    ledger: TaskDayLedger,
    owner: TaskGroupDailyMessageSlot,
    *,
    plan_total: int,
    source_hash: str,
) -> SourcePacingSlot:
    period_start = max(
        wall_datetime(ledger.period_start_at),
        wall_datetime(ledger.planning_anchor_at),
    )
    pacing_ordinal = (
        int(owner.pacing_slot_ordinal)
        if owner.pacing_slot_ordinal is not None
        else max(0, int(owner.slot_ordinal) - 1)
    )
    return SourcePacingSlot(
        source_key=str(ledger.id),
        slot_key=f"ai:{owner.id}",
        slot_ordinal=pacing_ordinal,
        plan_total=plan_total,
        period_start_at=period_start,
        deadline_at=wall_datetime(ledger.deadline_at),
        release_not_before_at=owner.release_not_before_at,
        owner_id=owner.id,
        task_lifecycle_epoch=int(task.task_lifecycle_epoch or 1),
        pacing_period_key=str(ledger.id),
        pacing_source_key_hash=source_hash,
        source_capacity_plan_hash=owner.source_capacity_plan_hash,
        source_capacity_slot_ordinal=owner.source_capacity_slot_ordinal,
    )


__all__ = ["AiPacingAssignment", "assign_ai_pacing_slots"]

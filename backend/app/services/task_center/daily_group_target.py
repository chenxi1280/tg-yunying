from __future__ import annotations

from datetime import date, datetime, time, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models import (
    Action,
    AiGroupMessageMemory,
    ExecutionAttempt,
    Task,
    TaskAccountDailyCoverage,
    TaskGroupDailyMessageSlot,
    TaskGroupDailyTarget,
    TaskMembershipAdmissionItem,
    TaskParticipationUnitPlan,
    TgGroup,
)
from app.services._common import _now
from .pacing import cumulative_pacing_due, task_pacing_anchor
from .engagement_daily_quantity import (
    POLICY_REVISION as UNIFIED_QUANTITY_POLICY_REVISION,
    group_ai_daily_quantity,
)


def effective_daily_message_target(configured: int, current_required: int) -> int:
    return max(1, int(configured or 0), int(current_required or 0))


def ensure_task_group_daily_target(
    session: Session,
    task: Task,
    group: TgGroup,
    target_date: date,
    *,
    now: datetime | None = None,
) -> TaskGroupDailyTarget:
    timestamp = now or _now()
    target = _locked_target(session, task, group, target_date)
    if target is None:
        target = _new_target(
            session,
            task,
            group,
            target_date=target_date,
            timestamp=timestamp,
        )
        session.add(target)
        session.flush()
    return refresh_task_group_daily_target(session, target)


def refresh_task_group_daily_target(
    session: Session,
    target: TaskGroupDailyTarget,
) -> TaskGroupDailyTarget:
    start = datetime.combine(target.target_date, time.min)
    end = start + timedelta(days=1)
    task = session.get(Task, target.task_id)
    if task is None:
        raise ValueError("task_group_daily_target_task_missing")
    current_required = _current_required_account_count(session, target)
    if target.quantity_policy_revision == UNIFIED_QUANTITY_POLICY_REVISION:
        configured = int(target.configured_message_target)
        planned = effective_daily_message_target(
            int(target.raw_quantity_target), current_required
        )
    else:
        configured = max(1, int((task.type_config or {}).get("daily_message_target") or 1))
        planned = effective_daily_message_target(configured, current_required)
    _apply_current_target(
        target,
        configured=configured,
        current_required=current_required,
        planned=planned,
    )
    target.confirmed_message_count = _confirmed_message_count(session, target, start, end)
    target.gateway_started_count = _gateway_started_count(
        session,
        target,
        start=start,
        end=end,
    )
    target.unknown_hold_count = _unknown_hold_count(
        session,
        target,
        start=start,
        end=end,
    )
    target.target_reduction_overage_count = max(
        0,
        target.confirmed_message_count
        + target.gateway_started_count
        + target.unknown_hold_count
        - planned,
    )
    target.coverage_confirmed_account_count = _coverage_confirmed_count(session, target)
    target.updated_at = _now()
    return target


def daily_group_due_message_count(
    target: TaskGroupDailyTarget,
    pacing_config: dict,
    *,
    anchor_at: datetime | None = None,
    now: datetime | None = None,
) -> int:
    timestamp = _wall_time(now or _now())
    day_start = datetime.combine(target.target_date, time.min)
    day_end = day_start + timedelta(days=1)
    anchors = [day_start, _wall_time(target.scope_frozen_at)]
    if anchor_at is not None:
        anchors.append(_wall_time(anchor_at))
    start = max(anchors)
    if timestamp <= start:
        return 0
    return cumulative_pacing_due(
        target.effective_message_target,
        pacing_config,
        anchor_at=start,
        period_start_at=day_start,
        period_end_at=day_end,
        now=timestamp,
    )


def _locked_target(
    session: Session,
    task: Task,
    group: TgGroup,
    target_date: date,
) -> TaskGroupDailyTarget | None:
    statement = select(TaskGroupDailyTarget).where(
        TaskGroupDailyTarget.tenant_id == task.tenant_id,
        TaskGroupDailyTarget.task_id == task.id,
        TaskGroupDailyTarget.group_id == group.id,
        TaskGroupDailyTarget.target_date == target_date,
    )
    return session.scalar(statement)


def _new_target(
    session: Session,
    task: Task,
    group: TgGroup,
    *,
    target_date: date,
    timestamp: datetime,
) -> TaskGroupDailyTarget:
    frozen_accounts = _frozen_account_count(session, task, group, target_date)
    participation = _daily_participation_plan(session, task, target_date)
    quantity = group_ai_daily_quantity(
        task,
        target_date,
        required_account_count=frozen_accounts,
        participation_plan=participation,
    )
    phase, committed_at = _fulfillment_phase(task, target_date)
    scope_frozen_at = _scope_frozen_at(
        task,
        target_date=target_date,
        fallback=timestamp,
        committed_at=committed_at,
        phase=phase,
    )
    return TaskGroupDailyTarget(
        tenant_id=task.tenant_id,
        task_id=task.id,
        group_id=group.id,
        target_date=target_date,
        participation_plan_id=participation.id if participation else None,
        configured_message_target=quantity.configured_target,
        quantity_policy_revision=quantity.policy_revision,
        quantity_seed=quantity.seed,
        sampled_jitter_bps=quantity.sampled_jitter_bps,
        raw_quantity_target=quantity.raw_target,
        frozen_account_count=frozen_accounts,
        effective_message_target=quantity.effective_target,
        planned_daily_target=quantity.effective_target,
        planned_target_revision=1,
        target_changed_at=timestamp,
        target_change_reason="created_from_current_task_scope",
        daily_fulfillment_phase=phase,
        scope_frozen_at=scope_frozen_at,
        full_day_committed_at=committed_at,
    )


def _daily_participation_plan(
    session: Session, task: Task, target_date: date
) -> TaskParticipationUnitPlan | None:
    return session.scalar(
        select(TaskParticipationUnitPlan).where(
            TaskParticipationUnitPlan.task_id == task.id,
            TaskParticipationUnitPlan.task_lifecycle_epoch == task.task_lifecycle_epoch,
            TaskParticipationUnitPlan.participation_kind == "group_daily_all",
            TaskParticipationUnitPlan.participation_unit
            == f"task_day:{target_date.isoformat()}",
            TaskParticipationUnitPlan.state == "active",
        )
    )


def _scope_frozen_at(
    task: Task,
    *,
    target_date: date,
    fallback: datetime,
    committed_at: datetime,
    phase: str,
) -> datetime:
    if phase != "admission_warming":
        return committed_at
    started_at = task_pacing_anchor(task)
    if started_at is None or _wall_time(started_at).date() != target_date:
        return fallback
    return _wall_time(started_at)


def _frozen_account_count(
    session: Session,
    task: Task,
    group: TgGroup,
    target_date: date,
) -> int:
    coverage_count = int(session.scalar(
        select(func.count(TaskAccountDailyCoverage.id)).where(
            TaskAccountDailyCoverage.tenant_id == task.tenant_id,
            TaskAccountDailyCoverage.task_id == task.id,
            TaskAccountDailyCoverage.group_id == group.id,
            TaskAccountDailyCoverage.coverage_date == target_date,
        )
    ) or 0)
    if coverage_count > 0:
        return coverage_count
    return int(session.scalar(
        select(func.count(TaskMembershipAdmissionItem.id)).where(
            TaskMembershipAdmissionItem.tenant_id == task.tenant_id,
            TaskMembershipAdmissionItem.task_id == task.id,
        )
    ) or 0)


def _current_required_account_count(
    session: Session,
    target: TaskGroupDailyTarget,
) -> int:
    total, required = session.execute(
        select(
            func.count(TaskAccountDailyCoverage.id),
            func.count(TaskAccountDailyCoverage.id).filter(
                TaskAccountDailyCoverage.state != "abandoned_for_day"
            ),
        ).where(
            TaskAccountDailyCoverage.tenant_id == target.tenant_id,
            TaskAccountDailyCoverage.task_id == target.task_id,
            TaskAccountDailyCoverage.group_id == target.group_id,
            TaskAccountDailyCoverage.coverage_date == target.target_date,
        )
    ).one()
    if int(total or 0) > 0:
        return int(required or 0)
    return int(session.scalar(select(func.count(TaskMembershipAdmissionItem.id)).where(
        TaskMembershipAdmissionItem.tenant_id == target.tenant_id,
        TaskMembershipAdmissionItem.task_id == target.task_id,
    )) or 0)


def _apply_current_target(
    target: TaskGroupDailyTarget,
    *,
    configured: int,
    current_required: int,
    planned: int,
) -> None:
    previous = int(target.planned_daily_target or target.effective_message_target or 1)
    previous_configured = int(target.configured_message_target or 1)
    target.configured_message_target = configured
    target.frozen_account_count = current_required
    target.effective_message_target = planned
    target.planned_daily_target = planned
    if planned == previous:
        return
    target.planned_target_revision = int(target.planned_target_revision or 1) + 1
    target.target_changed_at = _now()
    target.target_change_reason = (
        "current_required_account_count_changed"
        if configured == previous_configured
        else "configured_daily_target_changed"
    )


def _gateway_started_count(
    session: Session,
    target: TaskGroupDailyTarget,
    *,
    start: datetime,
    end: datetime,
) -> int:
    return int(session.scalar(
        select(func.count(func.distinct(Action.id)))
        .join(ExecutionAttempt, ExecutionAttempt.action_id == Action.id)
        .outerjoin(
            TaskGroupDailyMessageSlot,
            TaskGroupDailyMessageSlot.id == Action.primary_quantity_slot_id,
        )
        .where(
            Action.task_id == target.task_id,
            Action.status == "executing",
            ExecutionAttempt.gateway_call_started_at >= start,
            ExecutionAttempt.gateway_call_started_at < end,
            or_(
                TaskGroupDailyMessageSlot.id.is_(None),
                TaskGroupDailyMessageSlot.quantity_credit_eligible.is_(True),
            ),
        )
    ) or 0)


def _unknown_hold_count(
    session: Session,
    target: TaskGroupDailyTarget,
    *,
    start: datetime,
    end: datetime,
) -> int:
    return int(session.scalar(
        select(func.count(Action.id))
        .outerjoin(
            TaskGroupDailyMessageSlot,
            TaskGroupDailyMessageSlot.id == Action.primary_quantity_slot_id,
        )
        .where(
            Action.task_id == target.task_id,
            Action.status == "unknown_after_send",
            Action.executed_at >= start,
            Action.executed_at < end,
            or_(
                TaskGroupDailyMessageSlot.id.is_(None),
                TaskGroupDailyMessageSlot.quantity_credit_eligible.is_(True),
            ),
        )
    ) or 0)


def _fulfillment_phase(task: Task, target_date: date) -> tuple[str, datetime]:
    day_start = datetime.combine(target_date, time.min)
    started_at = task_pacing_anchor(task)
    if started_at and _wall_time(started_at).date() == target_date and _wall_time(started_at) > day_start:
        return "admission_warming", day_start + timedelta(days=1)
    return "full_day_committed", day_start


def _confirmed_message_count(
    session: Session,
    target: TaskGroupDailyTarget,
    start: datetime,
    end: datetime,
) -> int:
    actions = list(session.scalars(
        select(Action)
        .outerjoin(
            TaskGroupDailyMessageSlot,
            TaskGroupDailyMessageSlot.id == Action.primary_quantity_slot_id,
        )
        .where(
            Action.tenant_id == target.tenant_id,
            Action.task_id == target.task_id,
            Action.action_type == "send_message",
            Action.status == "success",
            Action.executed_at >= start,
            Action.executed_at < end,
            or_(
                TaskGroupDailyMessageSlot.id.is_(None),
                TaskGroupDailyMessageSlot.quantity_credit_eligible.is_(True),
            ),
            select(ExecutionAttempt.id).where(
                ExecutionAttempt.action_id == Action.id,
                ExecutionAttempt.status == "success",
                ExecutionAttempt.remote_message_id != "",
                ExecutionAttempt.account_id == Action.account_id,
            ).exists(),
        )
    ))
    memory_ids = {
        str((action.payload or {}).get("ai_message_memory_id") or "")
        for action in actions
    }
    memories = {
        memory.id: memory
        for memory in session.scalars(select(AiGroupMessageMemory).where(
            AiGroupMessageMemory.id.in_(memory_ids - {""}),
        ))
    }
    return sum(
        1
        for action in actions
        if _content_evidence_valid(
            action,
            memories.get(str((action.payload or {}).get("ai_message_memory_id") or "")),
        )
    )


def _content_evidence_valid(
    action: Action,
    memory: AiGroupMessageMemory | None,
) -> bool:
    payload = action.payload or {}
    if not memory or memory.action_id != action.id or memory.account_id != action.account_id:
        return False
    source = str(payload.get("content_source") or "")
    if source == "mask_missing_check_in":
        return bool(
            payload.get("coverage_ledger_id")
            and memory.content_source == source
            and memory.mask_status == "missing"
        )
    return bool(
        payload.get("account_mask_id")
        and int(payload.get("account_mask_version") or 0) > 0
        and memory.account_mask_id == payload.get("account_mask_id")
        and memory.mask_status == "active"
        and int(memory.account_mask_version or 0) == int(payload.get("account_mask_version") or 0)
        and memory.mask_contract_version == payload.get("voice_profile_contract_version")
        and memory.mask_snapshot_hash == payload.get("account_mask_snapshot_hash")
    )


def _coverage_confirmed_count(session: Session, target: TaskGroupDailyTarget) -> int:
    return int(session.scalar(
        select(func.count(TaskAccountDailyCoverage.id)).where(
            TaskAccountDailyCoverage.tenant_id == target.tenant_id,
            TaskAccountDailyCoverage.task_id == target.task_id,
            TaskAccountDailyCoverage.group_id == target.group_id,
            TaskAccountDailyCoverage.coverage_date == target.target_date,
            TaskAccountDailyCoverage.confirmed_count >= 1,
        )
    ) or 0)


def _wall_time(value: datetime) -> datetime:
    return value.replace(tzinfo=None) if value.tzinfo else value


__all__ = [
    "effective_daily_message_target",
    "daily_group_due_message_count",
    "ensure_task_group_daily_target",
    "refresh_task_group_daily_target",
]

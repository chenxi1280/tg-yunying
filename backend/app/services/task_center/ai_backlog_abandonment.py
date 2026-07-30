from __future__ import annotations

import json
from collections import Counter
from datetime import datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.models import (
    Action,
    AiCoverageVariationIntent,
    AuditLog,
    ContentMixCycle,
    ContentMixCycleSlot,
    ContentMixObligation,
    ExecutionAttempt,
    ReviewQueue,
    TaskAccountDailyCoverage,
    TaskGroupDailyMessageSlot,
    TaskHardHourlyDeliveryCredit,
)
from app.services._common import _now


ABANDONMENT_CODE = "operator_abandoned_historical_backlog"
ABANDONABLE_STATUSES = frozenset(
    {"pending", "claiming", "executing", "retryable_failed"}
)


def abandon_ai_historical_backlog(
    session: Session,
    *,
    cutoff: datetime,
    apply: bool,
    actor: str,
) -> dict:
    actions = _candidate_actions(session, cutoff=cutoff, lock=apply)
    result = _result(actions, cutoff=cutoff, apply=apply)
    if not apply or not actions:
        return result
    action_ids = [action.id for action in actions]
    _write_audits(session, actions, cutoff=cutoff, actor=actor)
    _terminalize_quantity_slots(session, actions)
    cycle_ids = _terminalize_content_mix_slots(session, actions)
    _shortfall_content_obligations(session, actions)
    _settle_finished_cycles(session, cycle_ids)
    _abandon_coverage(session, action_ids)
    _detach_variation_intents(session, action_ids)
    _delete_action_dependencies(session, action_ids)
    deleted = session.execute(
        delete(Action).where(Action.id.in_(action_ids))
    )
    return {**result, "deleted_action_count": int(deleted.rowcount or 0)}


def _candidate_actions(
    session: Session,
    *,
    cutoff: datetime,
    lock: bool,
) -> list[Action]:
    gateway_started = select(ExecutionAttempt.id).where(
        ExecutionAttempt.action_id == Action.id,
        ExecutionAttempt.gateway_call_started_at.is_not(None),
    ).exists()
    statement = select(Action).where(
        Action.task_type == "group_ai_chat",
        Action.action_type == "send_message",
        Action.scheduled_at < cutoff,
        Action.status.in_(ABANDONABLE_STATUSES),
        func.coalesce(
            Action.result["gateway_call_started_at"].as_string(),
            "",
        ) == "",
        ~gateway_started,
    ).order_by(Action.scheduled_at, Action.created_at, Action.id)
    if lock and session.bind and session.bind.dialect.name != "sqlite":
        statement = statement.with_for_update(of=Action, skip_locked=True)
    return list(session.scalars(statement))


def _result(
    actions: list[Action],
    *,
    cutoff: datetime,
    apply: bool,
) -> dict:
    return {
        "mode": "apply" if apply else "preview",
        "cutoff": cutoff.isoformat(),
        "candidate_count": len(actions),
        "deleted_action_count": 0,
        "status_counts": dict(Counter(action.status for action in actions)),
        "task_counts": dict(Counter(action.task_id for action in actions)),
    }


def _write_audits(
    session: Session,
    actions: list[Action],
    *,
    cutoff: datetime,
    actor: str,
) -> None:
    timestamp = _now()
    by_task: dict[tuple[int, str], list[Action]] = {}
    for action in actions:
        by_task.setdefault((action.tenant_id, action.task_id), []).append(action)
    for (tenant_id, task_id), task_actions in by_task.items():
        session.add(AuditLog(
            tenant_id=tenant_id,
            actor=actor,
            action="放弃并删除AI活群历史积压",
            target_type="task",
            target_id=task_id,
            detail=json.dumps(
                {
                    "reason_code": ABANDONMENT_CODE,
                    "cutoff": cutoff.isoformat(),
                    "action_count": len(task_actions),
                    "status_counts": dict(
                        Counter(action.status for action in task_actions)
                    ),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            ip_address="",
            created_at=timestamp,
        ))


def _terminalize_quantity_slots(
    session: Session,
    actions: list[Action],
) -> None:
    ids = [
        action.primary_quantity_slot_id
        for action in actions
        if action.primary_quantity_slot_id
    ]
    if ids:
        session.execute(
            update(TaskGroupDailyMessageSlot)
            .where(TaskGroupDailyMessageSlot.id.in_(ids))
            .values(state="terminal")
        )


def _terminalize_content_mix_slots(
    session: Session,
    actions: list[Action],
) -> list[str]:
    ids = [
        action.content_mix_cycle_slot_id
        for action in actions
        if action.content_mix_cycle_slot_id
    ]
    if not ids:
        return []
    cycle_ids = list(session.scalars(
        select(ContentMixCycleSlot.cycle_id).where(
            ContentMixCycleSlot.id.in_(ids)
        )
    ))
    session.execute(
        update(ContentMixCycleSlot)
        .where(ContentMixCycleSlot.id.in_(ids))
        .values(
            slot_state="terminal",
            terminal_reason=ABANDONMENT_CODE,
            current_action_id=None,
        )
    )
    return cycle_ids


def _shortfall_content_obligations(
    session: Session,
    actions: list[Action],
) -> None:
    slot_ids = [
        action.content_mix_cycle_slot_id
        for action in actions
        if action.content_mix_cycle_slot_id
    ]
    if not slot_ids:
        return
    remaining = ContentMixObligation.required_count - ContentMixObligation.success_count
    session.execute(
        update(ContentMixObligation)
        .where(ContentMixObligation.assigned_cycle_slot_id.in_(slot_ids))
        .values(
            status="shortfall",
            shortfall_count=func.greatest(
                ContentMixObligation.shortfall_count,
                remaining,
            ),
            assigned_action_id=None,
            assignment_version=ContentMixObligation.assignment_version + 1,
        )
    )


def _settle_finished_cycles(
    session: Session,
    cycle_ids: list[str],
) -> None:
    for cycle_id in set(cycle_ids):
        open_slot = session.scalar(
            select(ContentMixCycleSlot.id).where(
                ContentMixCycleSlot.cycle_id == cycle_id,
                ContentMixCycleSlot.slot_state.not_in(("confirmed", "terminal")),
            ).limit(1)
        )
        if open_slot is not None:
            continue
        session.execute(
            update(ContentMixCycle)
            .where(
                ContentMixCycle.id == cycle_id,
                ContentMixCycle.settlement_status != "settled",
            )
            .values(
                settlement_status="settled",
                settlement_outcome="shortfall",
                settled_at=_now(),
            )
        )


def _abandon_coverage(
    session: Session,
    action_ids: list[str],
) -> None:
    session.execute(
        update(TaskAccountDailyCoverage)
        .where(TaskAccountDailyCoverage.reserved_action_id.in_(action_ids))
        .values(
            state="abandoned",
            reserved_action_id=None,
            last_action_id=None,
            blocker_code=ABANDONMENT_CODE,
            blocker_stage="operator",
            blocker_detail="运营明确放弃本期历史积压，不重建、不补发",
            recovery_path="",
            next_eligible_at=None,
            next_decision_at=None,
            updated_at=_now(),
        )
    )


def _detach_variation_intents(
    session: Session,
    action_ids: list[str],
) -> None:
    session.execute(
        update(AiCoverageVariationIntent)
        .where(AiCoverageVariationIntent.action_id.in_(action_ids))
        .values(
            action_id=None,
            outcome=ABANDONMENT_CODE,
            updated_at=_now(),
        )
    )


def _delete_action_dependencies(
    session: Session,
    action_ids: list[str],
) -> None:
    session.execute(
        delete(ReviewQueue).where(ReviewQueue.action_id.in_(action_ids))
    )
    session.execute(
        delete(ExecutionAttempt).where(ExecutionAttempt.action_id.in_(action_ids))
    )
    session.execute(
        delete(TaskHardHourlyDeliveryCredit).where(
            TaskHardHourlyDeliveryCredit.action_id.in_(action_ids)
        )
    )


__all__ = [
    "ABANDONMENT_CODE",
    "abandon_ai_historical_backlog",
]

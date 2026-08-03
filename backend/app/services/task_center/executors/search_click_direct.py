from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    Action,
    SearchClickAssignment,
    SearchClickFulfillmentObligation,
    SearchProtocolSession,
    Task,
    TaskDayLedger,
    TgAccountAuthorization,
)
from app.config import get_settings

from ..payloads import create_search_join_action
from ..daily_ledgers import ensure_task_day_ledger
from ..search_click_assignment_solver import solve_search_click_assignments
from ..search_click_demands import record_task_opportunities, search_click_demands
from ..search_click_dispatch_allocation import SearchClickFulfillmentUnit
from ..search_click_outcome_identity import search_path_snapshot_hash
from .search_click_candidates import candidate_paths
from .search_join_group import _payload


def build_fact_first_plan(session: Session, task: Task, now: datetime) -> int:
    ensure_task_day_ledger(session, task, now=now)
    units = _open_units(session, now, _free_search_slots(session))
    if not units:
        return 0
    paths = candidate_paths(session, units, now)
    result = solve_search_click_assignments(
        search_click_demands(session, units),
        tuple(context.candidate for context in paths.values()),
    )
    created = _bind_matches(
        session,
        units=units,
        paths=paths,
        result=result,
        now=now,
    )
    record_task_opportunities(
        session,
        [unit.task_id for unit in units if unit.obligation_id in created],
        now_value=now,
    )
    session.commit()
    return sum(1 for unit in units if unit.task_id == task.id and unit.obligation_id in created)


def _free_search_slots(session: Session) -> int:
    configured = max(1, int(get_settings().search_dispatcher_concurrency or 1))
    occupied = int(session.scalar(
        select(func.count(Action.id))
        .join(Task, Task.id == Action.task_id)
        .where(
            Task.fulfillment_contract_version == "fact_first_v3",
            Action.execution_lane == "search",
            Action.status.in_(("pending", "claiming", "executing")),
        )
    ) or 0)
    return max(0, configured - occupied)


def _open_units(
    session: Session,
    now: datetime,
    limit: int,
) -> tuple[SearchClickFulfillmentUnit, ...]:
    if limit <= 0:
        return ()
    rows = session.execute(
        select(Task, TaskDayLedger, SearchClickFulfillmentObligation)
        .join(TaskDayLedger, TaskDayLedger.task_id == Task.id)
        .join(
            SearchClickFulfillmentObligation,
            SearchClickFulfillmentObligation.task_day_ledger_id == TaskDayLedger.id,
        )
        .where(
            Task.type == "search_click",
            Task.status == "running",
            Task.fulfillment_contract_version == "fact_first_v3",
            Task.deleted_at.is_(None),
            TaskDayLedger.lifecycle_status == "open",
            TaskDayLedger.deadline_at > now,
            SearchClickFulfillmentObligation.status == "open",
        )
        .order_by(SearchClickFulfillmentObligation.click_obligation_ordinal, Task.id)
        .limit(limit)
    ).all()
    return tuple(
        SearchClickFulfillmentUnit(obligation.id, task.id, "", "", 0, 0)
        for task, _, obligation in rows
    )


def _bind_matches(
    session: Session,
    *,
    units,
    paths,
    result,
    now: datetime,
) -> set[str]:
    matches = {match.obligation_id: match for match in result.matches}
    created: set[str] = set()
    for unit in units:
        match = matches.get(unit.obligation_id)
        path = paths.get(match.candidate_key) if match else None
        if path is not None and _bind_one(
            session,
            unit=unit,
            path=path,
            solver_hash=result.solver_input_hash,
            now=now,
        ):
            created.add(unit.obligation_id)
    return created


def _bind_one(
    session: Session,
    *,
    unit,
    path,
    solver_hash: str,
    now: datetime,
) -> bool:
    obligation = session.get(SearchClickFulfillmentObligation, unit.obligation_id)
    task = session.get(Task, unit.task_id)
    authorization = session.get(TgAccountAuthorization, path.candidate.authorization_id)
    ledger = session.get(TaskDayLedger, obligation.task_day_ledger_id) if obligation else None
    if any(value is None for value in (obligation, task, authorization, ledger)):
        return False
    if obligation.status != "open":
        return False
    assignment = _assignment(
        unit,
        task=task,
        path=path,
        solver_hash=solver_hash,
        obligation=obligation,
        authorization=authorization,
        deadline_at=ledger.deadline_at,
    )
    try:
        with session.begin_nested():
            session.add(assignment)
            session.flush()
    except IntegrityError:
        return False
    action = _action(
        session,
        task,
        obligation=obligation,
        assignment=assignment,
        path=path,
        now=now,
    )
    assignment.action_id = action.id
    assignment.state = "action_bound"
    obligation.source_action_id = action.id
    obligation.status = "action_bound"
    return True


def _assignment(
    unit,
    *,
    task: Task,
    path,
    solver_hash,
    obligation,
    authorization,
    deadline_at,
) -> SearchClickAssignment:
    return SearchClickAssignment(
        tenant_id=obligation.tenant_id,
        task_id=unit.task_id,
        task_lifecycle_epoch=int(task.task_lifecycle_epoch or 1),
        obligation_id=obligation.id,
        account_id=path.candidate.account_id,
        authorization_id=path.candidate.authorization_id,
        keyword_hash=path.candidate.keyword_hash,
        proxy_route_id=path.candidate.proxy_route_id,
        protocol_sample_version=path.candidate.protocol_sample_version,
        resource_snapshot_hash=search_path_snapshot_hash(path.candidate),
        solver_input_hash=solver_hash,
        obligation_deadline_at=deadline_at,
        binding_version=int(authorization.fact_version or 1),
    )


def _action(session, task, *, obligation, assignment, path, now):
    payload = _payload(path.payload_input).model_copy(update={
        "search_click_obligation_id": obligation.id,
        "search_click_assignment_id": assignment.id,
    })
    action = create_search_join_action(
        session, task, path.candidate.account_id, now, payload
    )
    action.execution_lane = "search"
    action.obligation_type = "search_click"
    action.obligation_id = obligation.id
    action.result = {
        "search_click_assignment_id": assignment.id,
        "dispatch_prebound": True,
        "fulfillment_contract_version": "fact_first_v3",
    }
    session.flush()
    target = path.payload_input.plan.target
    target_ref = "" if target is None else str(
        target.tg_peer_id or target.username or target.id
    )
    session.add(SearchProtocolSession(
        assignment_id=assignment.id,
        task_lifecycle_epoch=int(task.task_lifecycle_epoch or 1),
        keyword_id=path.candidate.keyword_hash,
        approved_target_ref=target_ref,
        protocol_sample_version=path.candidate.protocol_sample_version,
        request_identity=f"{assignment.id}:1",
        protocol_state={"phase": "assignment_created"},
    ))
    return action


__all__ = ["build_fact_first_plan"]

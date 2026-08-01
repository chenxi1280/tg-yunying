from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy.orm import Session

from app.models import (
    Action,
    DispatchClaimReservation,
    DispatchClaimScope,
    DispatchClaimShardAllocation,
    DispatchClaimWindow,
    OperationTarget,
    SearchClickAssignmentEpoch,
    SearchClickFulfillmentObligation,
    SearchClickOpportunityAssignment,
    Task,
    TaskDayLedger,
    Tenant,
    TgAccount,
)
from app.services._common import _now


@dataclass(frozen=True)
class CoreRows:
    task: Task
    ledger: TaskDayLedger
    obligation: SearchClickFulfillmentObligation


@dataclass(frozen=True)
class BindingRows:
    epoch: SearchClickAssignmentEpoch
    action: Action


def seed_assignment(session: Session) -> None:
    now_value = _now()
    session.add(Tenant(id=1, name="tenant"))
    session.add(TgAccount(
        id=1,
        tenant_id=1,
        display_name="account",
        phone_masked="+861***0001",
        status="在线",
    ))
    core = _add_core_rows(session, now_value)
    _add_dispatch_rows(session, now_value)
    binding = BindingRows(
        epoch=_add_epoch(session, now_value),
        action=_add_action(session, core, now_value),
    )
    _add_assignment(session, core, binding)
    session.commit()


def _add_core_rows(session: Session, now_value) -> CoreRows:
    target = OperationTarget(
        id=1,
        tenant_id=1,
        target_type="group",
        tg_peer_id="target_group",
        username="target_group",
        title="target",
    )
    session.add(target)
    task = Task(
        id="task-1",
        tenant_id=1,
        name="click",
        type="search_click",
        status="running",
    )
    session.add(task)
    ledger = TaskDayLedger(
        id="ledger-1",
        tenant_id=1,
        task_id=task.id,
        timezone_snapshot="Asia/Shanghai",
        timezone_revision=1,
        obligation_local_date=now_value.date(),
        period_start_at=now_value,
        deadline_at=now_value + timedelta(days=1),
        day_phase="full_day",
        planning_anchor_at=now_value,
    )
    session.add(ledger)
    obligation = SearchClickFulfillmentObligation(
        id="obligation-1",
        tenant_id=1,
        task_day_ledger_id=ledger.id,
        target_id=target.id,
        click_obligation_ordinal=1,
        status="action_bound",
        source_action_id="action-1",
    )
    session.add(obligation)
    return CoreRows(task=task, ledger=ledger, obligation=obligation)


def _add_epoch(session: Session, now_value) -> SearchClickAssignmentEpoch:
    epoch = SearchClickAssignmentEpoch(
        id="epoch-1",
        dispatch_claim_window_id="window-1",
        dispatch_allocation_epoch=1,
        solver_owner_lease_id="worker-1",
        solver_fencing_token="token-1",
        solver_claimed_at=now_value,
        solver_problem_hash="a" * 64,
        solver_input_hash="b" * 64,
        outcome="optimal",
        finalize_status="finalized",
    )
    session.add(epoch)
    return epoch


def _add_action(
    session: Session,
    core: CoreRows,
    now_value,
) -> Action:
    action = Action(
        id="action-1",
        tenant_id=1,
        task_id=core.task.id,
        task_type="search_click",
        action_type="search_join",
        account_id=1,
        status="pending",
        scheduled_at=now_value,
        payload={
            "search_click_assignment_id": "assignment-1",
            "search_click_obligation_id": core.obligation.id,
        },
        result={
            "dispatch_prebound": True,
            "search_click_assignment_id": "assignment-1",
        },
    )
    session.add(action)
    return action


def _add_assignment(
    session: Session,
    core: CoreRows,
    binding: BindingRows,
) -> None:
    session.add(SearchClickOpportunityAssignment(
        id="assignment-1",
        tenant_id=1,
        task_id=core.task.id,
        task_day_ledger_id=core.ledger.id,
        obligation_id=core.obligation.id,
        search_click_assignment_epoch_id=binding.epoch.id,
        dispatch_claim_reservation_id="reservation-1",
        fulfillment_lane_claim_ordinal=1,
        account_id=1,
        authorization_id=1,
        keyword_hash="c" * 64,
        proxy_route_id="proxy-1",
        protocol_sample_version="v1",
        resource_snapshot_hash="d" * 64,
        action_id=binding.action.id,
        state="action_bound",
    ))


def _add_dispatch_rows(session: Session, now_value) -> None:
    session.add(DispatchClaimScope(
        id="scope-1",
        dispatcher_scope="task_center_dispatch",
        claim_capacity=1,
    ))
    session.add(DispatchClaimWindow(
        id="window-1",
        dispatcher_scope="task_center_dispatch",
        bucket_start=now_value - timedelta(seconds=1),
        bucket_end=now_value + timedelta(minutes=1),
        claim_capacity=1,
        unclaimed_allocated_count=1,
        effective_unclaimed_count=1,
        allocation_epoch=1,
        allocation_state="ready",
    ))
    session.add(DispatchClaimShardAllocation(
        id="shard-1",
        dispatch_claim_window_id="window-1",
        dispatch_allocation_epoch=1,
        account_shard_total=1,
        account_shard_index=0,
        unclaimed_allocated_count=1,
    ))
    session.add(DispatchClaimReservation(
        id="reservation-1",
        dispatch_claim_shard_allocation_id="shard-1",
        dispatch_allocation_epoch=1,
        tenant_id=1,
        task_id="task-1",
        claim_class="search_join",
        bucket_start=now_value,
        required_claims=1,
        reserved_claims=1,
        bound_count=1,
    ))

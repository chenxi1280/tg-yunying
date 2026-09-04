from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AccountGroupMembershipSnapshotSet,
    Action,
    ChannelCommentPlanContract,
    ChannelMessage,
    CommentFulfillmentObligation,
    ReactionFulfillmentObligation,
    TaskAccountGroupBindingSetRevision,
    TaskDayLedger,
    TaskGroupDailyTarget,
    TaskParticipationUnitPlan,
    TgAccount,
    ViewAccountSourceAllocationPlan,
    ViewFulfillmentObligation,
)
from app.timezone import as_beijing
from .reaction_source_identity import reaction_source_identity as _reaction_source_identity


@dataclass(frozen=True)
class FrozenAccountOrigin:
    account_pool_id: int
    binding: TaskAccountGroupBindingSetRevision
    provenance: str
    participation_plan_id: str = ""


def resolve_frozen_account_origin(
    session: Session,
    action: Action,
    account: TgAccount,
) -> FrozenAccountOrigin:
    plan = _lineage_plan(session, action)
    if plan is None:
        plan = _unique_matching_plan(session, action, account.id)
    if plan is not None:
        _validate_plan_owner(action, plan)
        return _origin_from_plan(session, plan, account.id)
    if _has_frozen_membership(session, action):
        raise ValueError("engagement_account_origin_plan_missing")
    return _compat_current_origin(session, action, account)


def _lineage_plan(
    session: Session,
    action: Action,
) -> TaskParticipationUnitPlan | None:
    payload = dict(action.payload or {})
    plan_id = _group_plan_id(session, payload)
    plan_id = plan_id or _comment_plan_id(session, payload)
    plan_id = plan_id or _view_plan_id(session, payload)
    if plan_id:
        plan = session.get(TaskParticipationUnitPlan, plan_id)
        if plan is None:
            raise ValueError("engagement_participation_plan_missing")
        return plan
    return _reaction_plan(session, action, payload)


def _group_plan_id(session: Session, payload: dict) -> str:
    target_id = str(payload.get("daily_group_target_id") or "")
    target = session.get(TaskGroupDailyTarget, target_id) if target_id else None
    return str(target.participation_plan_id or "") if target else ""


def _comment_plan_id(session: Session, payload: dict) -> str:
    obligation_id = str(payload.get("comment_fulfillment_obligation_id") or "")
    obligation = session.get(CommentFulfillmentObligation, obligation_id) if obligation_id else None
    contract = (
        session.get(ChannelCommentPlanContract, obligation.plan_contract_id)
        if obligation is not None and obligation.plan_contract_id
        else None
    )
    if contract is None:
        return ""
    return str(
        contract.source_participation_plan_id
        or contract.daily_participation_plan_id
        or ""
    )


def _view_plan_id(session: Session, payload: dict) -> str:
    obligation_id = str(payload.get("view_fulfillment_obligation_id") or "")
    obligation = session.get(ViewFulfillmentObligation, obligation_id) if obligation_id else None
    if obligation is None:
        return ""
    allocation = session.scalar(
        select(ViewAccountSourceAllocationPlan)
        .where(
            ViewAccountSourceAllocationPlan.task_day_ledger_id
            == obligation.task_day_ledger_id,
        )
        .order_by(ViewAccountSourceAllocationPlan.allocation_revision.desc())
        .limit(1)
    )
    return str(allocation.participation_plan_id or "") if allocation else ""


def _reaction_plan(
    session: Session,
    action: Action,
    payload: dict,
) -> TaskParticipationUnitPlan | None:
    obligation_id = str(payload.get("reaction_fulfillment_obligation_id") or "")
    obligation = session.get(ReactionFulfillmentObligation, obligation_id) if obligation_id else None
    message = session.get(ChannelMessage, obligation.channel_message_id) if obligation else None
    if message is None:
        return None
    source = _reaction_source_identity(message)
    return _matching_source_plan(session, action, source)


def _matching_source_plan(
    session: Session,
    action: Action,
    source_identity: str,
) -> TaskParticipationUnitPlan | None:
    task_day = as_beijing(action.scheduled_at).date()
    unit = f"task_day:{task_day.isoformat()}:source:{source_identity}"
    return session.scalar(select(TaskParticipationUnitPlan).where(
        TaskParticipationUnitPlan.task_id == action.task_id,
        TaskParticipationUnitPlan.task_lifecycle_epoch == action.task_lifecycle_epoch,
        TaskParticipationUnitPlan.participation_unit == unit,
        TaskParticipationUnitPlan.state == "active",
    ))


def _unique_matching_plan(
    session: Session,
    action: Action,
    account_id: int,
) -> TaskParticipationUnitPlan | None:
    plans = _task_day_plans(session, action)
    matches = [
        plan for plan in plans
        if account_id in {int(item) for item in plan.selected_account_ids or []}
        and str(account_id) in (plan.selected_origin_groups or {})
    ]
    origins = {
        int(plan.selected_origin_groups[str(account_id)])
        for plan in matches
        if str(account_id) in (plan.selected_origin_groups or {})
    }
    if len(origins) > 1:
        raise ValueError("engagement_account_origin_ambiguous")
    return matches[0] if matches else None


def _validate_plan_owner(
    action: Action,
    plan: TaskParticipationUnitPlan,
) -> None:
    if (
        plan.tenant_id != action.tenant_id
        or plan.task_id != action.task_id
        or plan.task_lifecycle_epoch != action.task_lifecycle_epoch
    ):
        raise ValueError("engagement_participation_plan_owner_mismatch")


def _task_day_plans(session: Session, action: Action) -> list[TaskParticipationUnitPlan]:
    task_day = as_beijing(action.scheduled_at).date()
    return list(session.scalars(
        select(TaskParticipationUnitPlan)
        .join(TaskDayLedger, TaskDayLedger.id == TaskParticipationUnitPlan.task_day_ledger_id)
        .where(
            TaskParticipationUnitPlan.task_id == action.task_id,
            TaskParticipationUnitPlan.task_lifecycle_epoch == action.task_lifecycle_epoch,
            TaskParticipationUnitPlan.state == "active",
            TaskDayLedger.obligation_local_date == task_day,
        )
        .order_by(TaskParticipationUnitPlan.created_at.desc())
    ))


def _origin_from_plan(
    session: Session,
    plan: TaskParticipationUnitPlan,
    account_id: int,
) -> FrozenAccountOrigin:
    pool_id = int((plan.selected_origin_groups or {}).get(str(account_id)) or 0)
    if pool_id <= 0:
        raise ValueError("engagement_account_origin_missing")
    snapshot = session.get(AccountGroupMembershipSnapshotSet, plan.membership_snapshot_set_id)
    binding = (
        session.get(TaskAccountGroupBindingSetRevision, snapshot.binding_set_revision_id)
        if snapshot else None
    )
    if binding is None or pool_id not in {int(item) for item in binding.account_group_ids or []}:
        raise ValueError("engagement_account_origin_binding_invalid")
    return FrozenAccountOrigin(pool_id, binding, "frozen_participation_plan", plan.id)


def _has_frozen_membership(session: Session, action: Action) -> bool:
    return session.scalar(select(AccountGroupMembershipSnapshotSet.id).where(
        AccountGroupMembershipSnapshotSet.task_id == action.task_id,
        AccountGroupMembershipSnapshotSet.task_lifecycle_epoch
        == action.task_lifecycle_epoch,
    ).limit(1)) is not None


def _compat_current_origin(
    session: Session,
    action: Action,
    account: TgAccount,
) -> FrozenAccountOrigin:
    binding = session.scalar(select(TaskAccountGroupBindingSetRevision).where(
        TaskAccountGroupBindingSetRevision.task_id == action.task_id,
        TaskAccountGroupBindingSetRevision.state == "active",
    ))
    pool_id = int(account.pool_id or 0)
    if binding is None or pool_id not in {int(item) for item in binding.account_group_ids or []}:
        raise ValueError("account_outside_active_binding")
    return FrozenAccountOrigin(pool_id, binding, "current_pool_compat_no_snapshot")


__all__ = ["FrozenAccountOrigin", "resolve_frozen_account_origin"]

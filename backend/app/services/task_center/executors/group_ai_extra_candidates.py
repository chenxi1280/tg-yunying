from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Action, TaskAccountDailyCoverage, TaskMembershipAdmissionItem
from app.services._common import _now
from app.timezone import beijing_day_bounds

from ..daily_coverage_planning import MAX_DAILY_COVERAGE_PLAN_BATCH


DAILY_GROUP_EXTRA_CANDIDATE_LIMIT = MAX_DAILY_COVERAGE_PLAN_BATCH


@dataclass(frozen=True)
class DailyGroupExtraCandidateSpec:
    tenant_id: int
    task_id: str
    group_id: int
    task_day_ledger_id: str
    coverage_date: date
    excluded_account_ids: frozenset[int] = frozenset()


def daily_group_extra_candidate_ids(
    session: Session,
    spec: DailyGroupExtraCandidateSpec,
    *,
    now: datetime | None = None,
) -> list[int]:
    timestamp = now or _now()
    success_counts = _daily_success_count_subquery(spec)
    statement = (
        select(TaskMembershipAdmissionItem)
        .join(
            TaskAccountDailyCoverage,
            TaskAccountDailyCoverage.membership_item_id
            == TaskMembershipAdmissionItem.id,
        )
        .outerjoin(
            success_counts,
            success_counts.c.account_id == TaskMembershipAdmissionItem.account_id,
        )
        .where(*_candidate_filters(spec))
        .order_by(
            TaskMembershipAdmissionItem.eligibility_rank.asc(),
            TaskMembershipAdmissionItem.planner_last_selected_at.asc().nullsfirst(),
            func.coalesce(success_counts.c.success_count, 0).asc(),
            TaskMembershipAdmissionItem.id.asc(),
        )
        .limit(DAILY_GROUP_EXTRA_CANDIDATE_LIMIT)
    )
    items = list(session.scalars(statement))
    for item in items:
        item.planner_last_selected_at = timestamp
    return [int(item.account_id) for item in items]


def _candidate_filters(
    spec: DailyGroupExtraCandidateSpec,
) -> list:
    filters = [
        TaskMembershipAdmissionItem.tenant_id == spec.tenant_id,
        TaskMembershipAdmissionItem.task_id == spec.task_id,
        TaskAccountDailyCoverage.tenant_id == spec.tenant_id,
        TaskAccountDailyCoverage.task_id == spec.task_id,
        TaskAccountDailyCoverage.group_id == spec.group_id,
        TaskAccountDailyCoverage.task_day_ledger_id == spec.task_day_ledger_id,
        TaskAccountDailyCoverage.coverage_date == spec.coverage_date,
        TaskAccountDailyCoverage.state == "confirmed",
        TaskAccountDailyCoverage.confirmed_count >= TaskAccountDailyCoverage.target_count,
    ]
    if spec.excluded_account_ids:
        filters.append(
            TaskMembershipAdmissionItem.account_id.not_in(spec.excluded_account_ids)
        )
    return filters


def _daily_success_count_subquery(spec: DailyGroupExtraCandidateSpec):
    day_start, _day_end = beijing_day_bounds(
        datetime.combine(spec.coverage_date, time.min),
    )
    return (
        select(
            Action.account_id.label("account_id"),
            func.count(Action.id).label("success_count"),
        )
        .where(
            Action.tenant_id == spec.tenant_id,
            Action.task_id == spec.task_id,
            Action.action_type == "send_message",
            Action.status == "success",
            Action.executed_at >= day_start,
            Action.account_id.is_not(None),
        )
        .group_by(Action.account_id)
        .subquery()
    )


__all__ = [
    "DAILY_GROUP_EXTRA_CANDIDATE_LIMIT",
    "DailyGroupExtraCandidateSpec",
    "daily_group_extra_candidate_ids",
]

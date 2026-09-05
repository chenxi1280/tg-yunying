from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AccountGroupMembershipSnapshotSet,
    Task,
    TaskDayLedger,
    TaskParticipationUnitPlan,
    TgAccount,
)

from .engagement_binding import (
    UNIFIED_ENGAGEMENT_CONTRACT_VERSION,
    freeze_membership_snapshot,
)
from .engagement_policy_scope import policy_eligible_member_ids


POLICY_REVISION = "engagement_participation_v2_fleet_debt"


@dataclass(frozen=True)
class ParticipationCount:
    final: int
    rounded: int
    minimum: int
    maximum: int


def ensure_daily_participation_plan(
    session: Session, task: Task, ledger: TaskDayLedger
) -> TaskParticipationUnitPlan | None:
    if not _unified(task):
        return None
    unit = f"task_day:{ledger.obligation_local_date.isoformat()}"
    if task.type == "channel_view":
        config = task.type_config or {}
        return _ensure_plan(
            session,
            task,
            ledger=ledger,
            participation_kind="view_daily_cohort",
            participation_unit=unit,
            ratio_range=(
                int(config.get("account_ratio_min_bps") or 8000),
                int(config.get("account_ratio_max_bps") or 9500),
            ),
            rolling_days=int(config.get("rolling_participation_days") or 3),
            strict_majority=True,
        )
    if task.type == "channel_comment":
        return _ensure_plan(
            session,
            task,
            ledger=ledger,
            participation_kind="comment_daily_all",
            participation_unit=unit,
        )
    if task.type == "group_ai_chat":
        return _ensure_plan(
            session,
            task,
            ledger=ledger,
            participation_kind="group_daily_all",
            participation_unit=unit,
        )
    return None


def ensure_source_participation_plan(
    session: Session,
    task: Task,
    ledger: TaskDayLedger,
    *,
    source_identity: str,
    required_count: int,
    eligible_account_ids: list[int] | None = None,
    rolling_days: int = 1,
) -> TaskParticipationUnitPlan | None:
    if not _unified(task):
        return None
    unit = f"task_day:{ledger.obligation_local_date.isoformat()}:source:{source_identity}"
    return _ensure_plan(
        session,
        task,
        ledger=ledger,
        participation_kind=f"{task.type}_source",
        participation_unit=unit,
        required_count=required_count,
        eligible_account_ids=eligible_account_ids,
        rolling_days=rolling_days,
    )


def selected_accounts_for_plan(
    session: Session, task: Task, plan: TaskParticipationUnitPlan
) -> list[TgAccount]:
    ids = [int(item) for item in plan.selected_account_ids or []]
    if not ids:
        return []
    rows = session.scalars(
        select(TgAccount).where(
            TgAccount.tenant_id == task.tenant_id,
            TgAccount.id.in_(ids),
        )
    )
    by_id = {row.id: row for row in rows}
    return [by_id[account_id] for account_id in ids if account_id in by_id]


def apply_journey_participation_selection(
    session: Session,
    task: Task,
    plan: TaskParticipationUnitPlan,
    *,
    selected_account_ids: list[int] | tuple[int, ...],
    journey_plan_id: str,
) -> TaskParticipationUnitPlan:
    selected = tuple(dict.fromkeys(int(item) for item in selected_account_ids))
    eligible = {int(item) for item in plan.policy_eligible_account_ids or []}
    if len(selected) > int(plan.required_count) or not set(selected) <= eligible:
        raise ValueError("journey_participation_selection_invalid")
    if (
        len(selected) == int(plan.required_count)
        and list(selected) == [int(item) for item in plan.selected_account_ids or []]
    ):
        return plan
    snapshot = session.get(
        AccountGroupMembershipSnapshotSet,
        plan.membership_snapshot_set_id,
    )
    if snapshot is None:
        raise ValueError("journey_participation_membership_snapshot_missing")
    origins = dict(snapshot.account_origin_groups or {})
    if any(str(account_id) not in origins for account_id in selected):
        raise ValueError("journey_participation_account_origin_missing")
    plan.state = "superseded"
    session.flush()
    successor = _journey_successor(
        task,
        plan,
        selected=selected,
        origins=origins,
        journey_plan_id=journey_plan_id,
    )
    session.add(successor)
    session.flush()
    return successor


def _journey_successor(
    task: Task,
    plan: TaskParticipationUnitPlan,
    *,
    selected: tuple[int, ...],
    origins: dict,
    journey_plan_id: str,
) -> TaskParticipationUnitPlan:
    selection_hash = _hash({
        "eligible": plan.policy_eligible_account_ids,
        "selected": selected,
        "seed": plan.selection_seed,
        "journey_plan_id": journey_plan_id,
    })
    return TaskParticipationUnitPlan(
        tenant_id=task.tenant_id, task_id=task.id,
        task_lifecycle_epoch=task.task_lifecycle_epoch,
        task_day_ledger_id=plan.task_day_ledger_id,
        membership_snapshot_set_id=plan.membership_snapshot_set_id,
        participation_kind=plan.participation_kind,
        participation_unit=plan.participation_unit,
        plan_revision=int(plan.plan_revision or 1) + 1,
        policy_revision=plan.policy_revision,
        policy_eligible_account_ids=list(plan.policy_eligible_account_ids or []),
        selected_account_ids=list(selected),
        selected_origin_groups={str(item): origins[str(item)] for item in selected},
        sampled_ratio_bps=plan.sampled_ratio_bps,
        rounded_selected_count=len(selected),
        participation_min_count=len(selected),
        participation_max_count=len(selected),
        realized_participation_bps=_realized_bps(
            len(plan.policy_eligible_account_ids or []), len(selected)
        ),
        integer_quantization_adjustment=False,
        required_count=len(selected),
        selection_seed=plan.selection_seed,
        selection_hash=selection_hash,
    )


def _ensure_plan(
    session: Session,
    task: Task,
    *,
    ledger: TaskDayLedger,
    participation_kind: str,
    participation_unit: str,
    ratio_range: tuple[int, int] | None = None,
    required_count: int | None = None,
    rolling_days: int = 1,
    strict_majority: bool = False,
    eligible_account_ids: list[int] | None = None,
) -> TaskParticipationUnitPlan:
    existing = _active_plan(
        session, task, kind=participation_kind, unit=participation_unit,
    )
    if existing is not None:
        return existing
    snapshot = freeze_membership_snapshot(
        session, task, participation_unit=participation_unit
    )
    policy_members = policy_eligible_member_ids(session, task, snapshot)
    eligible = _eligible_ids(policy_members, eligible_account_ids)
    sampled_ratio = _sample_ratio(task.id, participation_unit, ratio_range)
    count = _participation_count(
        len(eligible), sampled_ratio, requested=required_count,
        strict_majority=strict_majority,
    )
    selected = _select_accounts(
        session, task, eligible,
        participation_kind=participation_kind,
        participation_unit=participation_unit,
        required_count=count.final,
        rolling_days=rolling_days,
        as_of=ledger.obligation_local_date,
    )
    plan = _new_plan(
        task,
        ledger,
        snapshot,
        participation_kind=participation_kind,
        participation_unit=participation_unit,
        eligible=eligible,
        selected=selected,
        sampled_ratio=sampled_ratio,
        count=count,
    )
    session.add(plan)
    session.flush()
    return plan


def _new_plan(
    task: Task,
    ledger: TaskDayLedger,
    snapshot,
    *,
    participation_kind: str,
    participation_unit: str,
    eligible: tuple[int, ...],
    selected: tuple[int, ...],
    sampled_ratio: int | None,
    count: ParticipationCount,
) -> TaskParticipationUnitPlan:
    seed = _hash({"task": task.id, "unit": participation_unit, "policy": POLICY_REVISION})
    origins = snapshot.account_origin_groups or {}
    return TaskParticipationUnitPlan(
        tenant_id=task.tenant_id,
        task_id=task.id,
        task_lifecycle_epoch=task.task_lifecycle_epoch,
        task_day_ledger_id=ledger.id,
        membership_snapshot_set_id=snapshot.id,
        participation_kind=participation_kind,
        participation_unit=participation_unit,
        policy_revision=POLICY_REVISION,
        policy_eligible_account_ids=list(eligible),
        selected_account_ids=list(selected),
        selected_origin_groups={str(item): origins[str(item)] for item in selected},
        sampled_ratio_bps=sampled_ratio,
        rounded_selected_count=count.rounded,
        participation_min_count=count.minimum,
        participation_max_count=count.maximum,
        realized_participation_bps=_realized_bps(len(eligible), count.final),
        integer_quantization_adjustment=count.final != count.rounded,
        required_count=count.final,
        selection_seed=seed,
        selection_hash=_hash({"eligible": eligible, "selected": selected, "seed": seed}),
    )


def _active_plan(
    session: Session,
    task: Task,
    *,
    kind: str,
    unit: str,
) -> TaskParticipationUnitPlan | None:
    return session.scalar(
        select(TaskParticipationUnitPlan).where(
            TaskParticipationUnitPlan.task_id == task.id,
            TaskParticipationUnitPlan.task_lifecycle_epoch == task.task_lifecycle_epoch,
            TaskParticipationUnitPlan.participation_kind == kind,
            TaskParticipationUnitPlan.participation_unit == unit,
            TaskParticipationUnitPlan.state == "active",
        )
    )


def _eligible_ids(
    snapshot_ids: list[int], requested_ids: list[int] | None
) -> tuple[int, ...]:
    members = tuple(int(item) for item in snapshot_ids)
    if requested_ids is None:
        return members
    requested = {int(item) for item in requested_ids}
    return tuple(account_id for account_id in members if account_id in requested)


def _sample_ratio(
    task_id: str, unit: str, ratio_range: tuple[int, int] | None
) -> int | None:
    if ratio_range is None:
        return None
    lower, upper = ratio_range
    if lower > upper:
        raise ValueError("account_ratio_min_bps 不能大于 account_ratio_max_bps")
    value = int(_hash({"task": task_id, "unit": unit})[:16], 16)
    return lower + value % (upper - lower + 1)


def _participation_count(
    eligible_count: int,
    sampled_ratio: int | None,
    *,
    requested: int | None,
    strict_majority: bool,
) -> ParticipationCount:
    if requested is not None:
        final = min(eligible_count, max(0, requested))
        return ParticipationCount(final, final, 0, eligible_count)
    rounded = eligible_count
    if sampled_ratio is not None:
        rounded = (eligible_count * sampled_ratio + 5000) // 10000
    minimum = eligible_count // 2 + 1 if strict_majority and eligible_count else 0
    final = min(eligible_count, max(minimum, rounded))
    return ParticipationCount(final, rounded, minimum, eligible_count)


def _realized_bps(eligible_count: int, selected_count: int) -> int | None:
    if eligible_count == 0:
        return None
    return (selected_count * 10000 + eligible_count // 2) // eligible_count


def _select_accounts(
    session: Session,
    task: Task,
    eligible: tuple[int, ...],
    *,
    participation_kind: str,
    participation_unit: str,
    required_count: int,
    rolling_days: int,
    as_of,
) -> tuple[int, ...]:
    task_debt = _selection_debt(
        session, task, kind=participation_kind, rolling_days=rolling_days,
    )
    from .engagement_fleet_activity import fleet_activity_selection_debt

    fleet_debt = fleet_activity_selection_debt(
        session,
        task.tenant_id,
        eligible,
        as_of=as_of,
        window_days=max(3, rolling_days),
    )
    ranked = sorted(
        eligible,
        key=lambda account_id: (
            fleet_debt.get(account_id, (0, 0)),
            task_debt[account_id],
            _hash({"task": task.id, "unit": participation_unit, "account": account_id}),
        ),
    )
    return tuple(ranked[:required_count])


def _selection_debt(
    session: Session,
    task: Task,
    *,
    kind: str,
    rolling_days: int,
) -> Counter:
    rows = session.scalars(
        select(TaskParticipationUnitPlan)
        .where(
            TaskParticipationUnitPlan.task_id == task.id,
            TaskParticipationUnitPlan.participation_kind == kind,
            TaskParticipationUnitPlan.state == "active",
        )
        .order_by(TaskParticipationUnitPlan.created_at.desc())
        .limit(max(0, rolling_days - 1))
    )
    return Counter(item for row in rows for item in row.selected_account_ids or [])


def _hash(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def _unified(task: Task) -> bool:
    return (
        (task.type_config or {}).get("engagement_contract_version")
        == UNIFIED_ENGAGEMENT_CONTRACT_VERSION
    )


__all__ = [
    "apply_journey_participation_selection",
    "ensure_daily_participation_plan",
    "ensure_source_participation_plan",
    "selected_accounts_for_plan",
]

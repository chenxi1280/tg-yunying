from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AccountBehaviorBudgetPolicyRevision,
    AccountPacingReservation,
    Action,
    FulfillmentRemoteFact,
    TgAccount,
)
from app.timezone import as_beijing

from .engagement_action_classes import action_class_for_type


OPEN_ACTION_STATUSES = (
    "pending",
    "claiming",
    "executing",
    "retryable_failed",
    "unknown_after_send",
)
INFLIGHT_ACTION_STATUSES = (
    "claiming",
    "executing",
    "retryable_failed",
    "unknown_after_send",
)
REMOTE_FACT_KINDS = (
    "remote_message_observed",
    "view_observed",
    "reaction_observed",
)


@dataclass(frozen=True)
class TypedPacingPoint:
    occurred_at: datetime
    action_class: str


def typed_account_policy_not_before(
    session: Session,
    *,
    tenant_id: int,
    account_id: int,
    candidate_class: str,
    desired_at: datetime,
    default_gap: timedelta,
    deadline_at: datetime | None,
    exclude_action_id: str | None,
    exclude_slot_key: str | None,
    include_planned: bool,
) -> datetime | None:
    policy = _active_policy(session, tenant_id, account_id)
    pair_gaps = _pair_gaps(policy)
    max_gap = max([default_gap, *pair_gaps.values()])
    normalized_deadline = as_beijing(deadline_at)
    points = _typed_points(
        session,
        tenant_id=tenant_id,
        account_id=account_id,
        start_at=desired_at - max_gap,
        end_at=(
            normalized_deadline + max_gap
            if normalized_deadline is not None
            else None
        ),
        exclude_action_id=exclude_action_id,
        exclude_slot_key=exclude_slot_key,
        include_planned=include_planned,
    )
    return _earliest_typed_time(
        desired_at,
        candidate_class=candidate_class,
        points=points,
        default_gap=default_gap,
        pair_gaps=pair_gaps,
    )


def _active_policy(
    session: Session,
    tenant_id: int,
    account_id: int,
) -> AccountBehaviorBudgetPolicyRevision:
    policy = session.scalar(
        select(AccountBehaviorBudgetPolicyRevision)
        .join(
            TgAccount,
            TgAccount.account_identity
            == AccountBehaviorBudgetPolicyRevision.account_class,
        )
        .where(
            TgAccount.id == account_id,
            TgAccount.tenant_id == tenant_id,
            AccountBehaviorBudgetPolicyRevision.tenant_id == tenant_id,
            AccountBehaviorBudgetPolicyRevision.state == "active",
        )
    )
    if policy is None:
        raise RuntimeError("account_behavior_budget_policy_missing")
    return policy


def _pair_gaps(
    policy: AccountBehaviorBudgetPolicyRevision,
) -> dict[str, timedelta]:
    result: dict[str, timedelta] = {}
    for key, raw_value in (policy.pair_gap_policy or {}).items():
        seconds = int(raw_value)
        if seconds < 0:
            raise RuntimeError("account_pair_gap_policy_invalid")
        result[str(key)] = timedelta(seconds=seconds)
    return result


def _typed_points(
    session: Session,
    *,
    tenant_id: int,
    account_id: int,
    start_at: datetime,
    end_at: datetime | None,
    exclude_action_id: str | None,
    exclude_slot_key: str | None,
    include_planned: bool,
) -> list[TypedPacingPoint]:
    points = _action_points(
        session,
        tenant_id=tenant_id,
        account_id=account_id,
        start_at=start_at,
        end_at=end_at,
        exclude_action_id=exclude_action_id,
        include_planned=include_planned,
    )
    points.extend(_fact_points(
        session,
        tenant_id=tenant_id,
        account_id=account_id,
        start_at=start_at,
        end_at=end_at,
        exclude_action_id=exclude_action_id,
    ))
    if include_planned:
        points.extend(_reservation_points(
            session,
            tenant_id=tenant_id,
            account_id=account_id,
            start_at=start_at,
            end_at=end_at,
            exclude_slot_key=exclude_slot_key,
        ))
    return sorted(points, key=lambda point: point.occurred_at)


def _action_points(
    session: Session,
    **scope,
) -> list[TypedPacingPoint]:
    filters = _time_filters(Action.scheduled_at, scope)
    filters.extend((
        Action.tenant_id == scope["tenant_id"],
        Action.account_id == scope["account_id"],
        Action.status.in_(OPEN_ACTION_STATUSES if scope["include_planned"] else INFLIGHT_ACTION_STATUSES),
    ))
    if scope["exclude_action_id"]:
        filters.append(Action.id != scope["exclude_action_id"])
    rows = session.execute(select(Action.scheduled_at, Action.action_type).where(*filters))
    return _normalize_rows(rows)


def _fact_points(session: Session, **scope) -> list[TypedPacingPoint]:
    filters = _time_filters(FulfillmentRemoteFact.observed_at, scope)
    filters.extend((
        FulfillmentRemoteFact.tenant_id == scope["tenant_id"],
        FulfillmentRemoteFact.fact_kind.in_(REMOTE_FACT_KINDS),
        Action.account_id == scope["account_id"],
    ))
    if scope["exclude_action_id"]:
        filters.append(FulfillmentRemoteFact.action_id != scope["exclude_action_id"])
    rows = session.execute(
        select(FulfillmentRemoteFact.observed_at, Action.action_type)
        .join(Action, Action.id == FulfillmentRemoteFact.action_id)
        .where(*filters)
    )
    return _normalize_rows(rows)


def _reservation_points(session: Session, **scope) -> list[TypedPacingPoint]:
    filters = _time_filters(AccountPacingReservation.effective_claim_at, scope)
    filters.extend((
        AccountPacingReservation.tenant_id == scope["tenant_id"],
        AccountPacingReservation.account_id == scope["account_id"],
        AccountPacingReservation.state.in_(("reserved", "bound")),
    ))
    if scope["exclude_slot_key"]:
        filters.append(AccountPacingReservation.pacing_slot_key != scope["exclude_slot_key"])
    rows = session.execute(
        select(
            AccountPacingReservation.effective_claim_at,
            AccountPacingReservation.action_class,
        ).where(*filters)
    )
    return _normalize_rows(rows, values_are_classes=True)


def _time_filters(column, scope: dict) -> list:
    filters = [column >= scope["start_at"]]
    if scope["end_at"] is not None:
        filters.append(column < scope["end_at"])
    return filters


def _normalize_rows(rows, *, values_are_classes: bool = False) -> list[TypedPacingPoint]:
    result = []
    for raw_at, raw_class in rows:
        occurred_at = as_beijing(raw_at)
        if occurred_at is None:
            continue
        action_class = str(raw_class or "") if values_are_classes else action_class_for_type(str(raw_class or ""))
        result.append(TypedPacingPoint(occurred_at, action_class))
    return result


def _earliest_typed_time(
    desired_at: datetime,
    *,
    candidate_class: str,
    points: list[TypedPacingPoint],
    default_gap: timedelta,
    pair_gaps: dict[str, timedelta],
) -> datetime | None:
    candidate = desired_at
    for point in points:
        if point.occurred_at <= candidate:
            candidate = max(
                candidate,
                point.occurred_at + _gap(point.action_class, candidate_class, default_gap, pair_gaps),
            )
            continue
        outgoing_gap = _gap(candidate_class, point.action_class, default_gap, pair_gaps)
        if candidate + outgoing_gap <= point.occurred_at:
            break
        candidate = point.occurred_at + _gap(
            point.action_class,
            candidate_class,
            default_gap,
            pair_gaps,
        )
    return candidate if points else None


def _gap(
    previous_class: str,
    next_class: str,
    default_gap: timedelta,
    pair_gaps: dict[str, timedelta],
) -> timedelta:
    key = f"{_family(previous_class)}_to_{_family(next_class)}_seconds"
    return max(default_gap, pair_gaps.get(key, timedelta()))


def _family(action_class: str) -> str:
    return "authored" if action_class.startswith("authored_") else "passive"


__all__ = ["TypedPacingPoint", "typed_account_policy_not_before"]

from __future__ import annotations

from collections.abc import Iterable, Iterator
from datetime import datetime, timedelta

from sqlalchemy import select, union_all
from sqlalchemy.orm import Session

from app.models import (
    AccountPacingReservation,
    Action,
    FulfillmentRemoteFact,
)
from app.timezone import as_beijing


TIMELINE_PAGE_SIZE = 128
OPEN_GUARD_STATUSES = (
    "pending",
    "claiming",
    "executing",
    "retryable_failed",
    "unknown_after_send",
)
INFLIGHT_GUARD_STATUSES = (
    "claiming",
    "executing",
    "retryable_failed",
    "unknown_after_send",
)
OPEN_RESERVATION_STATES = ("reserved", "bound")


def account_timeline_points(
    session: Session,
    tenant_id: int,
    account_id: int | None,
    *,
    desired_at: datetime,
    gap: timedelta,
    deadline_at: datetime | None,
    exclude_action_id: str | None,
    exclude_slot_key: str | None,
    task_id: str | None = None,
    include_planned: bool = True,
) -> Iterator[datetime]:
    normalized_deadline = as_beijing(deadline_at)
    timeline = _timeline_union(
        tenant_id=tenant_id,
        account_id=account_id,
        start_at=desired_at - gap,
        end_at=normalized_deadline + gap if normalized_deadline else None,
        exclude_action_id=exclude_action_id,
        exclude_slot_key=exclude_slot_key,
        task_id=task_id,
        include_planned=include_planned,
    )
    yield from _paged_timeline(session, timeline)


def _paged_timeline(session: Session, timeline) -> Iterator[datetime]:
    cursor: datetime | None = None
    while True:
        statement = (
            select(timeline.c.timeline_at)
            .distinct()
            .order_by(timeline.c.timeline_at)
            .limit(TIMELINE_PAGE_SIZE)
        )
        if cursor is not None:
            statement = statement.where(timeline.c.timeline_at > cursor)
        page = [
            point
            for value in session.scalars(statement)
            if (point := as_beijing(value)) is not None
        ]
        if not page:
            return
        yield from page
        if len(page) < TIMELINE_PAGE_SIZE:
            return
        cursor = page[-1]


def _timeline_union(
    *,
    tenant_id: int,
    account_id: int | None,
    start_at: datetime,
    end_at: datetime | None,
    exclude_action_id: str | None,
    exclude_slot_key: str | None,
    task_id: str | None,
    include_planned: bool,
):
    action_scope = (
        Action.task_id == task_id
        if task_id is not None
        else Action.account_id == account_id
    )
    reservation_scope = (
        AccountPacingReservation.task_id == task_id
        if task_id is not None
        else AccountPacingReservation.account_id == account_id
    )
    filters = _timeline_filters(
        tenant_id=tenant_id,
        action_scope=action_scope,
        reservation_scope=reservation_scope,
        start_at=start_at,
        end_at=end_at,
        exclude_action_id=exclude_action_id,
        exclude_slot_key=exclude_slot_key,
        include_planned=include_planned,
    )
    branches = [
        select(Action.scheduled_at.label("timeline_at")).where(*filters[0]),
        select(FulfillmentRemoteFact.observed_at.label("timeline_at"))
        .join(Action, Action.id == FulfillmentRemoteFact.action_id)
        .where(*filters[1]),
    ]
    if include_planned:
        branches.append(
            select(AccountPacingReservation.effective_claim_at.label("timeline_at"))
            .where(*filters[2])
        )
    return union_all(*branches).subquery()


def _timeline_filters(
    *,
    tenant_id: int,
    action_scope,
    reservation_scope,
    start_at: datetime,
    end_at: datetime | None,
    exclude_action_id: str | None,
    exclude_slot_key: str | None,
    include_planned: bool,
) -> tuple[list, list, list]:
    action_filters = [
        Action.tenant_id == tenant_id,
        action_scope,
        Action.status.in_(
            OPEN_GUARD_STATUSES if include_planned else INFLIGHT_GUARD_STATUSES
        ),
        Action.scheduled_at.is_not(None),
        Action.scheduled_at >= start_at,
    ]
    fact_filters = [
        FulfillmentRemoteFact.tenant_id == tenant_id,
        action_scope,
        FulfillmentRemoteFact.fact_kind.in_((
            "remote_message_observed",
            "view_observed",
            "reaction_observed",
        )),
        FulfillmentRemoteFact.observed_at >= start_at,
    ]
    reservation_filters = [
        AccountPacingReservation.tenant_id == tenant_id,
        reservation_scope,
        AccountPacingReservation.state.in_(OPEN_RESERVATION_STATES),
        AccountPacingReservation.effective_claim_at >= start_at,
    ]
    _append_identity_filters(
        action_filters,
        fact_filters,
        reservation_filters,
        exclude_action_id=exclude_action_id,
        exclude_slot_key=exclude_slot_key,
    )
    _append_end_filters(
        action_filters,
        fact_filters,
        reservation_filters,
        end_at=end_at,
    )
    return action_filters, fact_filters, reservation_filters


def _append_identity_filters(
    action_filters: list,
    fact_filters: list,
    reservation_filters: list,
    *,
    exclude_action_id: str | None,
    exclude_slot_key: str | None,
) -> None:
    if exclude_action_id:
        action_filters.append(Action.id != exclude_action_id)
        fact_filters.append(FulfillmentRemoteFact.action_id != exclude_action_id)
    if exclude_slot_key:
        reservation_filters.append(
            AccountPacingReservation.pacing_slot_key != exclude_slot_key
        )


def _append_end_filters(
    action_filters: list,
    fact_filters: list,
    reservation_filters: list,
    *,
    end_at: datetime | None,
) -> None:
    if end_at is None:
        return
    action_filters.append(Action.scheduled_at < end_at)
    fact_filters.append(FulfillmentRemoteFact.observed_at < end_at)
    reservation_filters.append(AccountPacingReservation.effective_claim_at < end_at)


def earliest_available_time(
    desired_at: datetime,
    timeline: Iterable[datetime],
    gap: timedelta,
) -> datetime | None:
    candidate = desired_at
    seen = False
    for point in timeline:
        seen = True
        if point + gap <= candidate:
            continue
        if candidate + gap <= point:
            break
        candidate = point + gap
    return candidate if seen else None


__all__ = ["account_timeline_points", "earliest_available_time"]

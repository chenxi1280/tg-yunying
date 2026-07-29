from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.models import (
    DispatchClaimReservation,
    DispatchClaimShardAllocation,
    DispatchClaimTaskAllocation,
    DispatchClaimWindow,
    SearchClickFulfillmentObligation,
)

from .search_click_dispatch_allocation import SearchClickFulfillmentUnit

FinalizeResult = TypeVar("FinalizeResult")


def restart_serializable_finalize_transaction(session: Session) -> None:
    session.rollback()
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        session.execute(text("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"))


def run_serializable_finalize(
    session: Session,
    finalize: Callable[[], FinalizeResult],
    abandon: Callable[[], None],
) -> FinalizeResult | None:
    try:
        result = finalize()
        session.commit()
    except DBAPIError as exc:
        if str(getattr(exc.orig, "sqlstate", "")) != "40001":
            raise
        session.rollback()
        abandon()
        session.commit()
        return None
    return result


def lock_search_finalize_inputs(
    session: Session,
    window_id: str,
    units: tuple[SearchClickFulfillmentUnit, ...],
) -> DispatchClaimWindow | None:
    window = session.scalar(
        select(DispatchClaimWindow)
        .where(DispatchClaimWindow.id == window_id)
        .with_for_update()
    )
    reservation_ids = sorted({unit.reservation_id for unit in units})
    reservations = list(session.scalars(
        select(DispatchClaimReservation)
        .where(DispatchClaimReservation.id.in_(reservation_ids))
        .order_by(DispatchClaimReservation.id)
    ))
    _lock_allocation_rows(session, reservations, reservation_ids)
    obligation_ids = sorted({unit.obligation_id for unit in units})
    list(session.scalars(
        select(SearchClickFulfillmentObligation)
        .where(SearchClickFulfillmentObligation.id.in_(obligation_ids))
        .order_by(SearchClickFulfillmentObligation.id)
        .with_for_update()
    ))
    return window


def _lock_allocation_rows(
    session: Session,
    reservations: list[DispatchClaimReservation],
    reservation_ids: list[str],
) -> None:
    task_allocation_ids = sorted({
        row.dispatch_claim_task_allocation_id
        for row in reservations
        if row.dispatch_claim_task_allocation_id
    })
    shard_allocation_ids = sorted({
        row.dispatch_claim_shard_allocation_id for row in reservations
    })
    list(session.scalars(
        select(DispatchClaimTaskAllocation)
        .where(DispatchClaimTaskAllocation.id.in_(task_allocation_ids))
        .order_by(DispatchClaimTaskAllocation.id)
        .with_for_update()
    ))
    list(session.scalars(
        select(DispatchClaimShardAllocation)
        .where(DispatchClaimShardAllocation.id.in_(shard_allocation_ids))
        .order_by(DispatchClaimShardAllocation.id)
        .with_for_update()
    ))
    list(session.scalars(
        select(DispatchClaimReservation)
        .where(DispatchClaimReservation.id.in_(reservation_ids))
        .order_by(DispatchClaimReservation.id)
        .with_for_update()
    ))


__all__ = [
    "lock_search_finalize_inputs",
    "restart_serializable_finalize_transaction",
    "run_serializable_finalize",
]

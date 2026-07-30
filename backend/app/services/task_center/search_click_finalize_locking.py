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
    allocation_epochs = sorted({
        unit.dispatch_allocation_epoch for unit in units
    })
    _lock_task_allocations(session, window_id, allocation_epochs)
    _lock_shard_allocations(session, window_id, allocation_epochs)
    reservation_ids = sorted({unit.reservation_id for unit in units})
    list(session.scalars(
        select(DispatchClaimReservation)
        .where(DispatchClaimReservation.id.in_(reservation_ids))
        .order_by(DispatchClaimReservation.id)
        .with_for_update()
    ))
    obligation_ids = sorted({unit.obligation_id for unit in units})
    list(session.scalars(
        select(SearchClickFulfillmentObligation)
        .where(SearchClickFulfillmentObligation.id.in_(obligation_ids))
        .order_by(SearchClickFulfillmentObligation.id)
        .with_for_update()
    ))
    return window


def _lock_task_allocations(
    session: Session,
    window_id: str,
    allocation_epochs: list[int],
) -> None:
    list(session.scalars(
        select(DispatchClaimTaskAllocation)
        .where(
            DispatchClaimTaskAllocation.dispatch_claim_window_id == window_id,
            DispatchClaimTaskAllocation.dispatch_allocation_epoch.in_(
                allocation_epochs
            ),
        )
        .order_by(DispatchClaimTaskAllocation.id)
        .with_for_update()
    ))


def _lock_shard_allocations(
    session: Session,
    window_id: str,
    allocation_epochs: list[int],
) -> None:
    list(session.scalars(
        select(DispatchClaimShardAllocation)
        .where(
            DispatchClaimShardAllocation.dispatch_claim_window_id == window_id,
            DispatchClaimShardAllocation.dispatch_allocation_epoch.in_(
                allocation_epochs
            ),
        )
        .order_by(DispatchClaimShardAllocation.id)
        .with_for_update()
    ))


__all__ = [
    "lock_search_finalize_inputs",
    "restart_serializable_finalize_transaction",
    "run_serializable_finalize",
]

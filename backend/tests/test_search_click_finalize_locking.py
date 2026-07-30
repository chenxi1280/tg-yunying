from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.task_center.search_click_dispatch_allocation import (
    SearchClickFulfillmentUnit,
)
from app.services.task_center.search_click_finalize_locking import (
    lock_search_finalize_inputs,
)


pytestmark = pytest.mark.no_postgres


class _LockCaptureSession:
    def __init__(self) -> None:
        self.table_order: list[str] = []

    def scalar(self, statement):
        self._capture(statement)
        return SimpleNamespace()

    def scalars(self, statement):
        self._capture(statement)
        return ()

    def _capture(self, statement) -> None:
        entity = statement.column_descriptions[0]["entity"]
        self.table_order.append(entity.__tablename__)


def test_finalize_never_prereads_reservation_before_parent_allocations() -> None:
    session = _LockCaptureSession()
    unit = SearchClickFulfillmentUnit(
        "obligation-1",
        "task-1",
        "reservation-1",
        "window-1",
        1,
        1,
    )

    lock_search_finalize_inputs(session, "window-1", (unit,))

    assert session.table_order == [
        "dispatch_claim_windows",
        "dispatch_claim_task_allocations",
        "dispatch_claim_shard_allocations",
        "dispatch_claim_reservations",
        "search_click_fulfillment_obligations",
    ]

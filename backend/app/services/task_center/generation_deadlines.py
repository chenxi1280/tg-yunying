"""Read the intersection of existing generation deadlines without extending any owner."""
from datetime import datetime
from collections.abc import Iterable

from sqlalchemy import select, tuple_

from app.models import (
    AccountPacingReservation, Action, FulfillmentObligationProjection,
    TaskDayLedger, TaskGroupDailyMessageSlot,
)
from app.timezone import as_beijing

from .datetime_compat import utc_storage_as_beijing_wall


PAYLOAD_DEADLINE_FIELDS = ("obligation_deadline_at", "deadline_at", "freshness_deadline_at")


def minimum_generation_deadline(values: Iterable[datetime | None]) -> datetime | None:
    deadlines = [as_beijing(value) for value in values if value is not None]
    return min(deadlines) if deadlines else None


def latest_safe_send_at(session, action: Action) -> datetime | None:
    return batch_latest_safe_send_at(session, (action,))


def batch_latest_safe_send_at(session, actions: Iterable[Action]) -> datetime | None:
    batch = tuple(actions)
    if not batch:
        return None
    values = [value for action in batch for value in _payload_deadlines(action)]
    return minimum_generation_deadline((
        *values, *_pacing_deadlines(session, batch), *_projection_deadlines(session, batch),
        *_quantity_day_deadlines(session, batch),
    ))


def _assert_scope(action: Action, row, *, owner: str) -> None:
    if row.tenant_id != action.tenant_id or row.task_id != action.task_id:
        raise ValueError(f"generation_deadline_scope_mismatch:{owner}")


def _pacing_deadlines(session, actions: tuple[Action, ...]) -> Iterable[datetime | None]:
    by_id = {action.id: action for action in actions}
    rows = session.scalars(select(AccountPacingReservation).where(
        AccountPacingReservation.action_id.in_(by_id),
    ))
    for row in rows:
        action = by_id[row.action_id]
        _assert_scope(action, row, owner="pacing")
        if row.account_id != action.account_id:
            raise ValueError("generation_deadline_scope_mismatch:pacing_account")
        yield row.source_deadline_at


def _projection_deadlines(session, actions: tuple[Action, ...]) -> Iterable[datetime | None]:
    keys = {(action.obligation_type, action.obligation_id) for action in actions if action.obligation_id}
    if not keys:
        return
    rows = session.scalars(select(FulfillmentObligationProjection).where(
        tuple_(FulfillmentObligationProjection.obligation_type, FulfillmentObligationProjection.obligation_id).in_(keys),
    ))
    by_key = {(row.obligation_type, row.obligation_id): row for row in rows}
    for action in actions:
        row = by_key.get((action.obligation_type, action.obligation_id))
        if row is None:
            continue
        _assert_scope(action, row, owner="projection")
        if int(row.task_lifecycle_epoch or 1) != int(action.task_lifecycle_epoch or 1):
            raise ValueError("generation_deadline_scope_mismatch:projection_epoch")
        yield row.deadline_at


def _quantity_day_deadlines(session, actions: tuple[Action, ...]) -> Iterable[datetime]:
    keys = {action.primary_quantity_slot_id for action in actions if action.primary_quantity_slot_id}
    if not keys:
        return
    rows = session.execute(select(TaskGroupDailyMessageSlot, TaskDayLedger).join(
        TaskDayLedger, TaskDayLedger.id == TaskGroupDailyMessageSlot.task_day_ledger_id,
    ).where(TaskGroupDailyMessageSlot.id.in_(keys)))
    by_id = {slot.id: (slot, ledger) for slot, ledger in rows}
    for action in actions:
        pair = by_id.get(action.primary_quantity_slot_id)
        if pair is None:
            continue
        slot, ledger = pair
        _assert_scope(action, slot, owner="quantity_slot")
        _assert_scope(action, ledger, owner="task_day")
        # daily_ledgers writes UTC instants; SQLite drops the offset on readback.
        yield utc_storage_as_beijing_wall(ledger.deadline_at)


def _payload_deadlines(action: Action) -> tuple[datetime, ...]:
    payload = dict(action.payload or {})
    values = []
    for field in PAYLOAD_DEADLINE_FIELDS:
        raw = payload.get(field)
        if raw is None:
            continue
        try:
            value = raw if isinstance(raw, datetime) else datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"generation_deadline_invalid:{field}") from exc
        values.append(value)
    return tuple(values)

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import (
    Action,
    ChannelMessage,
    Task,
    TaskDayLedger,
    ViewFulfillmentObligation,
)

from .channel_payloads import ViewMessagePayload


class RemoteFactAlreadyFulfilled(ValueError):
    pass


def assert_view_obligation_identity(
    session: Session,
    action: Action,
    *,
    payload: ViewMessagePayload,
    obligation: ViewFulfillmentObligation,
    task: Task,
    message: ChannelMessage,
) -> None:
    ledger = session.get(TaskDayLedger, obligation.task_day_ledger_id)
    payload_ledger_id = str(payload.task_day_ledger_id or "")
    identifiers_match = bool(
        ledger
        and action.account_id is not None
        and obligation.tenant_id == action.tenant_id == task.tenant_id
        and ledger.tenant_id == action.tenant_id
        and ledger.task_id == task.id
        and obligation.channel_message_id == message.id
        and obligation.account_id == int(action.account_id)
        and (not payload_ledger_id or payload_ledger_id == obligation.task_day_ledger_id)
        and (not action.obligation_id or str(action.obligation_id) == obligation.id)
        and (
            not payload.view_fulfillment_obligation_id
            or payload.view_fulfillment_obligation_id == obligation.id
        )
    )
    if not identifiers_match:
        raise ValueError("view_obligation_identity_mismatch")


__all__ = ["RemoteFactAlreadyFulfilled", "assert_view_obligation_identity"]

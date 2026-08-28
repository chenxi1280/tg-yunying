from __future__ import annotations

import hashlib
from datetime import datetime

from sqlalchemy.orm import Session

from app.models import (
    Action,
    ChannelMessage,
    Task,
    TaskDayLedger,
    ViewFulfillmentObligation,
)

from .channel_payloads import ViewMessagePayload
from .daily_ledgers import ensure_task_day_ledger


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


def assert_existing_view_obligation_identity(
    session: Session,
    action: Action,
    *,
    payload: ViewMessagePayload,
    obligation: ViewFulfillmentObligation | None,
    task: Task,
    message: ChannelMessage,
) -> None:
    if obligation is None:
        return
    assert_view_obligation_identity(
        session,
        action,
        payload=payload,
        obligation=obligation,
        task=task,
        message=message,
    )


def resolve_view_action_ledger(
    session: Session,
    task: Task,
    payload: ViewMessagePayload,
    obligation: ViewFulfillmentObligation | None,
    *,
    now: datetime,
) -> TaskDayLedger:
    ledger_id = (
        obligation.task_day_ledger_id
        if obligation is not None
        else str(payload.task_day_ledger_id or "")
    )
    ledger = session.get(TaskDayLedger, ledger_id) if ledger_id else None
    if ledger is None and ledger_id:
        raise ValueError("view_obligation_ledger_missing")
    if ledger is None:
        ledger = ensure_task_day_ledger(session, task, now=now)
    if ledger.task_id != task.id or ledger.tenant_id != task.tenant_id:
        raise ValueError("view_obligation_ledger_identity_mismatch")
    return ledger


def stamp_view_payload(
    action: Action,
    payload: ViewMessagePayload,
    ledger: TaskDayLedger,
    *,
    obligation_id: str = "",
) -> None:
    payload.execution_date = ledger.obligation_local_date.isoformat()
    payload.task_day_ledger_id = ledger.id
    if obligation_id:
        payload.view_fulfillment_obligation_id = obligation_id
    action.payload = payload.model_dump(mode="json")


def reaction_state_revision(reaction_emoji: str) -> str:
    normalized = reaction_emoji.strip()
    if not normalized:
        raise ValueError("reaction_emoji_missing")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def evidence_hash(kind: str, *parts: object) -> str:
    source = ":".join([kind, *(str(part) for part in parts)])
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def assert_fact_owner(actual_obligation_id: str, expected_obligation_id: str) -> None:
    if actual_obligation_id != expected_obligation_id:
        raise ValueError("remote_fact_owned_by_another_obligation")


__all__ = [
    "RemoteFactAlreadyFulfilled",
    "assert_existing_view_obligation_identity",
    "assert_fact_owner",
    "assert_view_obligation_identity",
    "evidence_hash",
    "reaction_state_revision",
    "resolve_view_action_ledger",
    "stamp_view_payload",
]

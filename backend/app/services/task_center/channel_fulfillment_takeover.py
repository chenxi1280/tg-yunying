from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Action,
    ChannelMessage,
    ReactionFulfillmentObligation,
    ReactionRemoteFact,
    Task,
    ViewRemoteFact,
)

from .channel_fulfillment import (
    bind_obligation_action,
    confirm_reaction_obligation,
    confirm_view_obligation,
    ensure_reaction_obligation,
    ensure_view_obligation,
)
from .channel_payloads import LikeMessagePayload, ViewMessagePayload
from .daily_ledgers import ensure_task_day_ledger


MIGRATABLE_ACTION_STATUSES = frozenset(
    {
        "pending",
        "claiming",
        "executing",
        "retryable_failed",
        "success",
        "unknown_after_send",
    }
)
REACTION_UNAVAILABLE_CODES = frozenset({"reaction_unavailable_message"})


@dataclass(frozen=True)
class ChannelTakeoverSummary:
    bound_action_count: int = 0
    backfilled_fact_count: int = 0
    duplicate_action_count: int = 0

    def plus(self, other: "ChannelTakeoverSummary") -> "ChannelTakeoverSummary":
        return ChannelTakeoverSummary(
            self.bound_action_count + other.bound_action_count,
            self.backfilled_fact_count + other.backfilled_fact_count,
            self.duplicate_action_count + other.duplicate_action_count,
        )


def migrate_channel_fulfillment(
    session: Session,
    task: Task,
    *,
    now: datetime,
) -> ChannelTakeoverSummary:
    action_type = _action_type(task.type)
    if not action_type:
        return ChannelTakeoverSummary()
    actions = session.scalars(
        select(Action)
        .where(
            Action.task_id == task.id,
            Action.action_type == action_type,
            Action.status.in_(_migratable_statuses(task.type)),
        )
        .order_by(Action.scheduled_at, Action.created_at, Action.id)
    )
    summary = ChannelTakeoverSummary()
    for action in actions:
        migrated = (
            _migrate_like_action(session, task, action)
            if task.type == "channel_like"
            else _migrate_view_action(session, task, action, now=now)
        )
        summary = summary.plus(migrated)
    return summary


def _migrate_like_action(
    session: Session,
    task: Task,
    action: Action,
) -> ChannelTakeoverSummary:
    if (
        action.status == "skipped"
        and str((action.result or {}).get("error_code") or "")
        not in REACTION_UNAVAILABLE_CODES
    ):
        return ChannelTakeoverSummary()
    payload = LikeMessagePayload(**(action.payload or {}))
    message = _message(session, task, action, payload.channel_message_id)
    if action.status == "skipped":
        _record_reaction_unavailable(task, action)
    existing_fact = _reaction_fact(session, payload, action)
    if existing_fact is not None:
        existing_obligation = session.get(
            ReactionFulfillmentObligation,
            existing_fact.obligation_id,
        )
        if existing_obligation is not None and existing_obligation.task_id == task.id:
            _stamp_like_payload(action, payload, existing_obligation)
        if action.status != "success":
            _retire_duplicate(action)
        return ChannelTakeoverSummary(duplicate_action_count=1)
    obligation = ensure_reaction_obligation(
        session,
        task,
        message,
        _account_id(action),
    )
    _stamp_like_payload(action, payload, obligation)
    if action.status == "success":
        if obligation.status != "confirmed" and not obligation.current_action_id:
            bind_obligation_action(obligation, action)
        confirm_reaction_obligation(
            session,
            obligation,
            target_peer_id=payload.channel_id,
            reaction_emoji=payload.reaction_emoji,
            confirmed_at=(
                action.executed_at
                or action.scheduled_at
                or action.created_at
            ),
        )
        return ChannelTakeoverSummary(backfilled_fact_count=1)
    return _bind_nonconfirmed_action(obligation, action)


def _migrate_view_action(
    session: Session,
    task: Task,
    action: Action,
    *,
    now: datetime,
) -> ChannelTakeoverSummary:
    payload = ViewMessagePayload(**(action.payload or {}))
    message = _message(session, task, action, payload.channel_message_id)
    existing_fact = _view_fact(session, payload, action)
    if existing_fact is not None:
        if action.status != "success":
            _retire_duplicate(action)
        return ChannelTakeoverSummary(duplicate_action_count=1)
    timestamp = (
        action.executed_at
        or action.scheduled_at
        or (action.created_at if action.status == "success" else now)
    )
    ledger = ensure_task_day_ledger(session, task, now=timestamp)
    obligation = ensure_view_obligation(
        session,
        ledger,
        message,
        _account_id(action),
    )
    _stamp_view_payload(action, payload, obligation, ledger)
    if action.status == "success":
        if not obligation.current_action_id:
            bind_obligation_action(obligation, action)
        confirm_view_obligation(
            session,
            obligation,
            target_peer_id=payload.channel_id,
            confirmed_at=(
                action.executed_at
                or action.scheduled_at
                or action.created_at
            ),
        )
        return ChannelTakeoverSummary(backfilled_fact_count=1)
    return _bind_nonconfirmed_action(obligation, action)


def _bind_nonconfirmed_action(
    obligation,
    action: Action,
) -> ChannelTakeoverSummary:
    if obligation.status == "confirmed":
        _retire_duplicate(action)
        return ChannelTakeoverSummary(duplicate_action_count=1)
    if obligation.current_action_id and obligation.current_action_id != action.id:
        _retire_duplicate(action)
        return ChannelTakeoverSummary(duplicate_action_count=1)
    bound = obligation.current_action_id != action.id
    if bound:
        bind_obligation_action(obligation, action)
    if action.status == "unknown_after_send":
        obligation.status = "unknown"
    if action.status == "skipped":
        obligation.status = "unavailable"
    return ChannelTakeoverSummary(bound_action_count=int(bound))


def _stamp_like_payload(action: Action, payload, obligation) -> None:
    payload.reaction_contract_version = obligation.reaction_contract_version
    payload.reaction_fulfillment_obligation_id = obligation.id
    action.payload = payload.model_dump(mode="json")


def _stamp_view_payload(action: Action, payload, obligation, ledger) -> None:
    payload.execution_date = ledger.obligation_local_date.isoformat()
    payload.task_day_ledger_id = ledger.id
    payload.view_fulfillment_obligation_id = obligation.id
    action.payload = payload.model_dump(mode="json")


def _reaction_fact(
    session: Session,
    payload,
    action: Action,
) -> ReactionRemoteFact | None:
    pending = next(
        (
            fact
            for fact in session.new
            if isinstance(fact, ReactionRemoteFact)
            and fact.target_peer_id == payload.channel_id
            and fact.channel_message_id == payload.channel_message_id
            and fact.account_id == action.account_id
        ),
        None,
    )
    if pending is not None:
        return pending
    return session.scalar(
        select(ReactionRemoteFact).where(
            ReactionRemoteFact.target_peer_id == payload.channel_id,
            ReactionRemoteFact.channel_message_id == payload.channel_message_id,
            ReactionRemoteFact.account_id == action.account_id,
        )
    )


def _view_fact(session: Session, payload, action: Action) -> ViewRemoteFact | None:
    pending = next(
        (
            fact
            for fact in session.new
            if isinstance(fact, ViewRemoteFact)
            and fact.target_peer_id == payload.channel_id
            and fact.channel_message_id == payload.channel_message_id
            and fact.account_id == action.account_id
        ),
        None,
    )
    if pending is not None:
        return pending
    return session.scalar(
        select(ViewRemoteFact).where(
            ViewRemoteFact.target_peer_id == payload.channel_id,
            ViewRemoteFact.channel_message_id == payload.channel_message_id,
            ViewRemoteFact.account_id == action.account_id,
        )
    )


def _message(
    session: Session,
    task: Task,
    action: Action,
    channel_message_id: int | None,
) -> ChannelMessage:
    message = session.get(ChannelMessage, int(channel_message_id or 0))
    if message is None or message.tenant_id != task.tenant_id:
        raise ValueError(f"channel_fulfillment_message_missing:{action.id}")
    return message


def _account_id(action: Action) -> int:
    if action.account_id is None:
        raise ValueError(f"channel_fulfillment_account_missing:{action.id}")
    return int(action.account_id)


def _retire_duplicate(action: Action) -> None:
    action.status = "skipped"
    action.result = {
        **(action.result or {}),
        "error_code": "remote_fact_already_fulfilled",
        "error_message": "同一远端事实已确认，不重复计入履约量",
    }
    action.claim_owner = ""
    action.claim_token = ""
    action.claim_expires_at = None


def _action_type(task_type: str) -> str:
    return {
        "channel_like": "like_message",
        "channel_view": "view_message",
    }.get(task_type, "")


def _migratable_statuses(task_type: str) -> frozenset[str]:
    if task_type == "channel_like":
        return MIGRATABLE_ACTION_STATUSES | {"skipped"}
    return MIGRATABLE_ACTION_STATUSES


def _record_reaction_unavailable(
    task: Task,
    action: Action,
) -> None:
    task.last_error = str(
        (action.result or {}).get("error_message")
        or "频道消息当前不可点赞"
    )


__all__ = ["ChannelTakeoverSummary", "migrate_channel_fulfillment"]

from __future__ import annotations

import hashlib
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Action,
    ChannelMessage,
    ReactionFulfillmentObligation,
    ReactionRemoteFact,
    Task,
    TaskDayLedger,
    ViewFulfillmentObligation,
    ViewRemoteFact,
)

from .channel_payloads import LikeMessagePayload, ViewMessagePayload
from .channel_fulfillment_queries import (
    reaction_account_ids_for_messages,
    reaction_source_held_by_other_action,
    view_account_ids_for_messages,
    view_confirmed_counts,
    view_daily_counts,
    view_materialized_account_ids_for_messages,
    view_source_held_by_other_action,
)
from .daily_ledgers import ensure_task_day_ledger


TERMINAL_REPLAN_STATUSES = frozenset({"failed", "skipped", "cancelled"})
class RemoteFactAlreadyFulfilled(ValueError):
    pass


def ensure_reaction_obligation(
    session: Session,
    task: Task,
    message: ChannelMessage,
    account_id: int,
) -> ReactionFulfillmentObligation:
    obligation = session.scalar(
        select(ReactionFulfillmentObligation).where(
            ReactionFulfillmentObligation.task_id == task.id,
            ReactionFulfillmentObligation.channel_message_id == message.id,
            ReactionFulfillmentObligation.account_id == account_id,
            ReactionFulfillmentObligation.reaction_contract_version
            == task.config_revision,
        )
    )
    if obligation is None:
        obligation = ReactionFulfillmentObligation(
            tenant_id=task.tenant_id,
            task_id=task.id,
            channel_message_id=message.id,
            account_id=account_id,
            reaction_contract_version=task.config_revision,
        )
        session.add(obligation)
        session.flush()
    _release_terminal_action(session, obligation)
    return obligation


def ensure_view_obligation(
    session: Session,
    ledger: TaskDayLedger,
    message: ChannelMessage,
    account_id: int,
) -> ViewFulfillmentObligation:
    obligation = session.scalar(
        select(ViewFulfillmentObligation).where(
            ViewFulfillmentObligation.task_day_ledger_id == ledger.id,
            ViewFulfillmentObligation.channel_message_id == message.id,
            ViewFulfillmentObligation.account_id == account_id,
        )
    )
    if obligation is None:
        obligation = ViewFulfillmentObligation(
            tenant_id=ledger.tenant_id,
            task_day_ledger_id=ledger.id,
            channel_message_id=message.id,
            account_id=account_id,
        )
        session.add(obligation)
        session.flush()
    _release_terminal_action(session, obligation)
    return obligation


def bind_obligation_action(
    obligation: ReactionFulfillmentObligation | ViewFulfillmentObligation,
    action: Action,
) -> None:
    if obligation.status == "confirmed":
        raise ValueError("fulfilled_obligation_cannot_be_rebound")
    if (
        obligation.current_action_id
        and obligation.current_action_id != action.id
        and obligation.status == "pending"
    ):
        raise ValueError("fulfillment_obligation_already_bound")
    obligation.current_action_id = action.id
    obligation.action_attempt_no = int(obligation.action_attempt_no or 0) + 1
    obligation.status = "pending"


def obligation_accepts_new_action(
    obligation: ReactionFulfillmentObligation | ViewFulfillmentObligation,
) -> bool:
    return obligation.status == "open" and obligation.current_action_id is None


def ensure_reaction_action_contract(
    session: Session,
    action: Action,
    payload: LikeMessagePayload,
) -> ReactionFulfillmentObligation:
    existing = session.scalar(
        select(ReactionRemoteFact).where(
            ReactionRemoteFact.tenant_id == action.tenant_id,
            ReactionRemoteFact.channel_message_id == payload.channel_message_id,
            ReactionRemoteFact.account_id == action.account_id,
        )
    )
    if existing is not None:
        if existing.obligation_id == payload.reaction_fulfillment_obligation_id:
            return session.get(ReactionFulfillmentObligation, existing.obligation_id)
        raise RemoteFactAlreadyFulfilled("reaction_remote_source_already_fulfilled")
    if reaction_source_held_by_other_action(
        session,
        action,
        int(payload.channel_message_id or 0),
    ):
        raise RemoteFactAlreadyFulfilled("reaction_remote_source_held")
    task, message = _task_and_message(session, action, payload.channel_message_id)
    obligation = ensure_reaction_obligation(session, task, message, _account_id(action))
    if obligation.current_action_id != action.id:
        bind_obligation_action(obligation, action)
    payload.reaction_contract_version = obligation.reaction_contract_version
    payload.reaction_fulfillment_obligation_id = obligation.id
    action.payload = payload.model_dump(mode="json")
    return obligation


def ensure_view_action_contract(
    session: Session,
    action: Action,
    payload: ViewMessagePayload,
    *,
    now: datetime,
) -> ViewFulfillmentObligation:
    existing = session.scalar(
        select(ViewRemoteFact).where(
            ViewRemoteFact.tenant_id == action.tenant_id,
            ViewRemoteFact.channel_message_id == payload.channel_message_id,
            ViewRemoteFact.account_id == action.account_id,
        )
    )
    if existing is not None:
        if existing.obligation_id == payload.view_fulfillment_obligation_id:
            return session.get(ViewFulfillmentObligation, existing.obligation_id)
        raise RemoteFactAlreadyFulfilled("view_remote_source_already_fulfilled")
    if view_source_held_by_other_action(
        session,
        action,
        int(payload.channel_message_id or 0),
    ):
        raise RemoteFactAlreadyFulfilled("view_remote_source_held")
    task, message = _task_and_message(session, action, payload.channel_message_id)
    ledger = ensure_task_day_ledger(session, task, now=now)
    obligation = ensure_view_obligation(session, ledger, message, _account_id(action))
    if obligation.current_action_id != action.id:
        bind_obligation_action(obligation, action)
    payload.execution_date = ledger.obligation_local_date.isoformat()
    payload.task_day_ledger_id = ledger.id
    payload.view_fulfillment_obligation_id = obligation.id
    action.payload = payload.model_dump(mode="json")
    return obligation


def confirm_reaction_obligation(
    session: Session,
    obligation: ReactionFulfillmentObligation,
    *,
    target_peer_id: str,
    reaction_emoji: str,
    confirmed_at: datetime,
) -> ReactionRemoteFact:
    state_revision = _reaction_state_revision(reaction_emoji)
    fact = session.scalar(
        select(ReactionRemoteFact).where(
            ReactionRemoteFact.target_peer_id == target_peer_id,
            ReactionRemoteFact.channel_message_id == obligation.channel_message_id,
            ReactionRemoteFact.account_id == obligation.account_id,
            ReactionRemoteFact.reaction_state_revision == state_revision,
        )
    )
    if fact is None:
        fact = ReactionRemoteFact(
            tenant_id=obligation.tenant_id,
            obligation_id=obligation.id,
            target_peer_id=target_peer_id,
            channel_message_id=obligation.channel_message_id,
            account_id=obligation.account_id,
            reaction_state_revision=state_revision,
            reaction_evidence_hash=_evidence_hash(
                "reaction",
                target_peer_id,
                obligation.channel_message_id,
                obligation.account_id,
                state_revision,
            ),
            remote_confirmed_at=confirmed_at,
        )
        session.add(fact)
    _assert_fact_owner(fact.obligation_id, obligation.id)
    obligation.status = "confirmed"
    return fact


def confirm_view_obligation(
    session: Session,
    obligation: ViewFulfillmentObligation,
    *,
    target_peer_id: str,
    confirmed_at: datetime,
) -> ViewRemoteFact:
    fact = session.scalar(
        select(ViewRemoteFact).where(
            ViewRemoteFact.target_peer_id == target_peer_id,
            ViewRemoteFact.channel_message_id == obligation.channel_message_id,
            ViewRemoteFact.account_id == obligation.account_id,
        )
    )
    if fact is None:
        fact = ViewRemoteFact(
            tenant_id=obligation.tenant_id,
            obligation_id=obligation.id,
            target_peer_id=target_peer_id,
            channel_message_id=obligation.channel_message_id,
            account_id=obligation.account_id,
            remote_confirmed_at=confirmed_at,
        )
        session.add(fact)
    _assert_fact_owner(fact.obligation_id, obligation.id)
    obligation.status = "confirmed"
    return fact


def confirm_reaction_action(
    session: Session,
    obligation_id: str,
    action_id: str,
    *,
    target_peer_id: str,
    reaction_emoji: str,
    confirmed_at: datetime,
) -> ReactionRemoteFact:
    obligation = _bound_obligation(
        session,
        ReactionFulfillmentObligation,
        obligation_id,
        action_id,
    )
    return confirm_reaction_obligation(
        session,
        obligation,
        target_peer_id=target_peer_id,
        reaction_emoji=reaction_emoji,
        confirmed_at=confirmed_at,
    )


def confirm_view_action(
    session: Session,
    obligation_id: str,
    action_id: str,
    *,
    target_peer_id: str,
    confirmed_at: datetime,
) -> ViewRemoteFact:
    obligation = _bound_obligation(
        session,
        ViewFulfillmentObligation,
        obligation_id,
        action_id,
    )
    return confirm_view_obligation(
        session,
        obligation,
        target_peer_id=target_peer_id,
        confirmed_at=confirmed_at,
    )


def release_channel_action_before_gateway(
    session: Session,
    action: Action,
) -> None:
    contract = {
        "like_message": (
            ReactionFulfillmentObligation,
            "reaction_fulfillment_obligation_id",
        ),
        "view_message": (
            ViewFulfillmentObligation,
            "view_fulfillment_obligation_id",
        ),
    }.get(action.action_type)
    if contract is None:
        return
    model, payload_key = contract
    payload = action.payload if isinstance(action.payload, dict) else {}
    obligation = session.get(model, str(payload.get(payload_key) or ""))
    if obligation is None or obligation.current_action_id != action.id:
        return
    if obligation.status == "confirmed":
        raise RuntimeError("confirmed_channel_obligation_cannot_reopen")
    obligation.current_action_id = None
    obligation.status = "open"


def _release_terminal_action(
    session: Session,
    obligation: ReactionFulfillmentObligation | ViewFulfillmentObligation,
) -> None:
    if not obligation.current_action_id or obligation.status == "confirmed":
        return
    action = session.get(Action, obligation.current_action_id)
    if action is not None and _action_bound_to_other_obligation(action, obligation):
        obligation.current_action_id = None
        obligation.status = "open"
        return
    if action is not None and action.status not in TERMINAL_REPLAN_STATUSES:
        return
    obligation.current_action_id = None
    obligation.status = "open"


def _action_bound_to_other_obligation(
    action: Action,
    obligation: ReactionFulfillmentObligation | ViewFulfillmentObligation,
) -> bool:
    payload = action.payload if isinstance(action.payload, dict) else {}
    binding_key = (
        "reaction_fulfillment_obligation_id"
        if isinstance(obligation, ReactionFulfillmentObligation)
        else "view_fulfillment_obligation_id"
    )
    bound_obligation_id = payload.get(binding_key)
    if bound_obligation_id:
        return str(bound_obligation_id) != str(obligation.id)
    return (
        action.account_id is not None
        and int(action.account_id) != int(obligation.account_id)
    )


def _task_and_message(
    session: Session,
    action: Action,
    channel_message_id: int | None,
) -> tuple[Task, ChannelMessage]:
    task = session.get(Task, action.task_id)
    if task is None:
        raise ValueError("fulfillment_task_missing")
    if not channel_message_id:
        raise ValueError("fulfillment_channel_message_id_missing")
    message = session.get(ChannelMessage, channel_message_id)
    if message is None or message.tenant_id != action.tenant_id:
        raise ValueError("fulfillment_channel_message_missing")
    return task, message


def _account_id(action: Action) -> int:
    if action.account_id is None:
        raise ValueError("fulfillment_account_id_missing")
    return int(action.account_id)


def _bound_obligation(session: Session, model, obligation_id: str, action_id: str):
    if not obligation_id:
        raise ValueError("fulfillment_obligation_id_missing")
    obligation = session.get(model, obligation_id)
    if obligation is None:
        raise ValueError("fulfillment_obligation_missing")
    if obligation.current_action_id != action_id:
        raise ValueError("fulfillment_obligation_action_mismatch")
    return obligation


def _reaction_state_revision(reaction_emoji: str) -> str:
    normalized = reaction_emoji.strip()
    if not normalized:
        raise ValueError("reaction_emoji_missing")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _evidence_hash(kind: str, *parts: object) -> str:
    source = ":".join([kind, *(str(part) for part in parts)])
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _assert_fact_owner(actual_obligation_id: str, expected_obligation_id: str) -> None:
    if actual_obligation_id != expected_obligation_id:
        raise ValueError("remote_fact_owned_by_another_obligation")


__all__ = [
    "bind_obligation_action",
    "confirm_reaction_action",
    "confirm_reaction_obligation",
    "confirm_view_action",
    "confirm_view_obligation",
    "ensure_reaction_action_contract",
    "ensure_reaction_obligation",
    "ensure_view_action_contract",
    "ensure_view_obligation",
    "obligation_accepts_new_action",
    "release_channel_action_before_gateway",
    "RemoteFactAlreadyFulfilled",
    "reaction_account_ids_for_messages",
    "view_account_ids_for_messages",
    "view_confirmed_counts",
    "view_daily_counts",
    "view_materialized_account_ids_for_messages",
]

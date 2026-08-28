from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AccountPacingReservation,
    Action,
    ChannelMessage,
    ExecutionAttempt,
    ReactionFulfillmentObligation,
    ReactionRemoteFact,
    Task,
    TaskDayLedger,
    ViewFulfillmentObligation,
    ViewRemoteFact,
)
from app.services._common import _now
from .channel_fulfillment_identity import RemoteFactAlreadyFulfilled
from .channel_fulfillment_identity import assert_existing_view_obligation_identity
from .channel_fulfillment_identity import assert_fact_owner
from .channel_fulfillment_identity import evidence_hash
from .channel_fulfillment_identity import reaction_state_revision
from .channel_fulfillment_identity import resolve_view_action_ledger
from .channel_fulfillment_identity import stamp_view_payload
from .channel_payloads import LikeMessagePayload, ViewMessagePayload
from .channel_fulfillment_queries import (
    reaction_account_ids_for_messages,
    reaction_source_held_by_other_action,
    view_account_ids_for_messages,
    view_confirmed_counts,
    view_daily_counts,
    view_materialized_account_ids_for_messages,
    view_remote_fact_for_date,
)
from .channel_view_daily_identity import (
    DailyIdentityClaim,
    claim_daily_identity,
    confirm_daily_identity,
)
TERMINAL_REPLAN_STATUSES = frozenset({"failed", "skipped", "cancelled"})
LIFECYCLE_ACTION_TYPES = frozenset({"like_message", "view_message"})


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
    task, message = _task_and_message(session, action, payload.channel_message_id)
    obligation_id = payload.view_fulfillment_obligation_id or getattr(action, "obligation_id", None)
    obligation = session.get(ViewFulfillmentObligation, str(obligation_id)) if obligation_id else None
    assert_existing_view_obligation_identity(
        session,
        action,
        payload=payload,
        obligation=obligation,
        task=task,
        message=message,
    )
    ledger = resolve_view_action_ledger(session, task, payload, obligation, now=now)
    action_date = ledger.obligation_local_date
    stamp_view_payload(action, payload, ledger)
    existing = view_remote_fact_for_date(
        session,
        tenant_id=action.tenant_id,
        channel_message_id=int(payload.channel_message_id or 0),
        account_id=_account_id(action),
        obligation_local_date=action_date,
    )
    if existing is not None:
        if existing.obligation_id == str(obligation_id or ""):
            return session.get(ViewFulfillmentObligation, existing.obligation_id)
        raise RemoteFactAlreadyFulfilled("view_remote_source_already_fulfilled")
    if obligation_id:
        if obligation is not None:
            if obligation.current_action_id != action.id:
                bind_obligation_action(obligation, action)
            _claim_view_action_identity(
                session,
                action,
                payload=payload,
                ledger=ledger,
                obligation=obligation,
            )
            return obligation
    obligation = ensure_view_obligation(session, ledger, message, _account_id(action))
    if obligation.current_action_id != action.id:
        bind_obligation_action(obligation, action)
    stamp_view_payload(action, payload, ledger, obligation_id=obligation.id)
    _claim_view_action_identity(
        session,
        action,
        payload=payload,
        ledger=ledger,
        obligation=obligation,
    )
    return obligation


def _claim_view_action_identity(
    session: Session,
    action: Action,
    *,
    payload: ViewMessagePayload,
    ledger: TaskDayLedger,
    obligation: ViewFulfillmentObligation,
) -> None:
    owner = claim_daily_identity(
        session,
        DailyIdentityClaim(
            tenant_id=action.tenant_id,
            logical_task_id=action.task_id,
            target_peer_id=payload.channel_id,
            channel_message_id=obligation.channel_message_id,
            account_id=_account_id(action),
            obligation_local_date=ledger.obligation_local_date,
            obligation_id=obligation.id,
            action_id=action.id,
        ),
    )
    if owner is None:
        raise RemoteFactAlreadyFulfilled("view_daily_identity_held")


def confirm_reaction_obligation(
    session: Session,
    obligation: ReactionFulfillmentObligation,
    *,
    target_peer_id: str,
    reaction_emoji: str,
    confirmed_at: datetime,
) -> ReactionRemoteFact:
    state_revision = reaction_state_revision(reaction_emoji)
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
            reaction_evidence_hash=evidence_hash(
                "reaction",
                target_peer_id,
                obligation.channel_message_id,
                obligation.account_id,
                state_revision,
            ),
            remote_confirmed_at=confirmed_at,
        )
        session.add(fact)
    assert_fact_owner(fact.obligation_id, obligation.id)
    obligation.status = "confirmed"
    return fact


def confirm_view_obligation(
    session: Session,
    obligation: ViewFulfillmentObligation,
    *,
    target_peer_id: str,
    confirmed_at: datetime,
) -> ViewRemoteFact:
    ledger = session.get(TaskDayLedger, obligation.task_day_ledger_id)
    if ledger is None:
        raise ValueError("view_obligation_ledger_missing")
    obligation_date = ledger.obligation_local_date
    fact = session.scalar(
        select(ViewRemoteFact).where(
            ViewRemoteFact.target_peer_id == target_peer_id,
            ViewRemoteFact.channel_message_id == obligation.channel_message_id,
            ViewRemoteFact.account_id == obligation.account_id,
            ViewRemoteFact.obligation_local_date == obligation_date,
        )
    )
    if fact is None:
        fact = ViewRemoteFact(
            tenant_id=obligation.tenant_id,
            obligation_id=obligation.id,
            obligation_local_date=obligation_date,
            target_peer_id=target_peer_id,
            channel_message_id=obligation.channel_message_id,
            account_id=obligation.account_id,
            remote_effect_kind="daily_view_operation",
            counter_increment_proven=False,
            remote_confirmed_at=confirmed_at,
        )
        session.add(fact)
    assert_fact_owner(fact.obligation_id, obligation.id)
    obligation.status = "confirmed"
    confirm_daily_identity(session, obligation, fact)
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


def cancel_superseded_channel_actions(session: Session, task: Task) -> int:
    gateway_started = select(ExecutionAttempt.id).where(
        ExecutionAttempt.action_id == Action.id,
        ExecutionAttempt.gateway_call_started_at.is_not(None),
    ).exists()
    actions = list(session.scalars(
        select(Action).where(
            Action.task_id == task.id,
            Action.action_type.in_(LIFECYCLE_ACTION_TYPES),
            Action.status == "pending",
            Action.task_lifecycle_epoch != task.task_lifecycle_epoch,
            ~gateway_started,
        )
    ))
    state_ids: set[str] = set()
    for action in actions:
        release_channel_action_before_gateway(session, action)
        action.status = "skipped"
        action.executed_at = action.executed_at or _now()
        action.action_version = int(action.action_version or 1) + 1
        action.result = {
            **dict(action.result or {}),
            "error_code": "task_lifecycle_superseded_pre_gateway",
            "remote_mutation_started": False,
        }
        state_ids.update(_release_superseded_reservations(session, action))
    if state_ids:
        from .direct_action_claims import reconcile_source_pacing_states

        reconcile_source_pacing_states(session, state_ids)
    return len(actions)


def _release_superseded_reservations(
    session: Session,
    action: Action,
) -> set[str]:
    reservation = session.scalar(select(AccountPacingReservation.id).where(
        AccountPacingReservation.action_id == action.id,
        AccountPacingReservation.state.in_(("reserved", "bound")),
    ))
    if reservation is None or not action.pacing_slot_key:
        return set()
    from .direct_action_claims import release_fact_first_action_reservations

    return release_fact_first_action_reservations(
        session,
        action,
        fact_kind="safely_not_executed",
    )


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

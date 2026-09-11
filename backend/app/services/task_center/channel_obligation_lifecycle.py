"""Shared terminal binding release rules for channel fulfillment owners."""
from sqlalchemy.orm import Session
from app.models import Action, ReactionFulfillmentObligation, ViewFulfillmentObligation

TERMINAL_REPLAN_STATUSES = frozenset({"failed", "skipped", "cancelled"})


def _release_terminal_action(
    session: Session,
    obligation: ReactionFulfillmentObligation | ViewFulfillmentObligation,
) -> None:
    if not obligation.current_action_id or obligation.status == "confirmed":
        return
    action = session.get(Action, obligation.current_action_id)
    release_obligation_action(obligation, action)


def release_obligation_action(obligation, action: Action | None) -> None:
    if not obligation.current_action_id or obligation.status == "confirmed":
        return
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



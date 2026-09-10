"""A typed channel confirmation and historical retry risk are separate facts."""
from sqlalchemy import select

from app.models import ExecutionAttempt, FulfillmentRemoteFact, ReactionRemoteFact, ViewRemoteFact
from .channel_fulfillment_identity import reaction_state_revision
from .datetime_compat import compare_datetimes

CHANNEL_FACT_KINDS = {"channel_like": "reaction_observed", "channel_view": "view_observed"}


def confirmed_channel_attempt(session, action):
    if action.status != "success":
        return None
    identifier = str((action.result or {}).get("remote_fact_id") or "")
    if not identifier:
        return None
    model = ReactionRemoteFact if action.action_type == "like_message" else ViewRemoteFact
    payload = action.payload or {}
    query = select(model).where(model.tenant_id == action.tenant_id,
        model.obligation_id == action.obligation_id, model.account_id == action.account_id,
        model.target_peer_id == str(payload.get("channel_id") or ""),
        model.channel_message_id == payload.get("channel_message_id"))
    if model is ReactionRemoteFact:
        query = query.where(model.reaction_state_revision == reaction_state_revision(
            str(payload.get("reaction_emoji") or "")))
    fact = session.scalar(query)
    if fact is None or not _matches_channel_owner(action, fact):
        return None
    candidates = session.scalars(select(ExecutionAttempt).where(
        ExecutionAttempt.action_id == action.id,
        ExecutionAttempt.tenant_id == action.tenant_id,
        ExecutionAttempt.account_id == action.account_id,
        ExecutionAttempt.task_lifecycle_epoch == action.task_lifecycle_epoch,
        ExecutionAttempt.status == "success",
        ExecutionAttempt.gateway_call_started_at.is_not(None),
        ExecutionAttempt.result_snapshot["remote_fact_id"].as_string() == identifier,
    ).order_by(ExecutionAttempt.attempt_no.desc()))
    return next((attempt for attempt in candidates if compare_datetimes(
        fact.remote_confirmed_at, attempt.gateway_call_started_at) >= 0), None)


def _matches_channel_owner(action, fact):
    payload = action.payload or {}
    if (fact.tenant_id, fact.obligation_id, fact.account_id) != (
        action.tenant_id, action.obligation_id, action.account_id,
    ):
        return False
    if str(fact.target_peer_id) != str(payload.get("channel_id") or ""):
        return False
    if fact.channel_message_id != payload.get("channel_message_id"):
        return False
    if isinstance(fact, ReactionRemoteFact):
        return fact.reaction_state_revision == reaction_state_revision(str(payload.get("reaction_emoji") or ""))
    return True


def preserve_channel_confirmation(session, fact, projection):
    if projection.state != "confirmed" or fact.fact_kind not in {"remote_outcome_unknown", "safely_not_executed"}:
        return False
    positive_kind = CHANNEL_FACT_KINDS.get(fact.task_type)
    if positive_kind is None:
        return False
    return session.scalar(select(FulfillmentRemoteFact.fact_id).where(
        FulfillmentRemoteFact.tenant_id == fact.tenant_id,
        FulfillmentRemoteFact.task_id == fact.task_id,
        FulfillmentRemoteFact.obligation_type == fact.obligation_type,
        FulfillmentRemoteFact.obligation_id == fact.obligation_id,
        FulfillmentRemoteFact.fact_kind == positive_kind,
    ).limit(1)) is not None

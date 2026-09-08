"""Prove an early call-issued projection never crossed a remote boundary."""
from sqlalchemy import select

from app.models import (
    ExecutionAttempt, FulfillmentRemoteFact, GatewayRequestEvidenceJournal,
    RemoteInvocationFence, ViewRemoteFact,
)

UNCALLED_TERMINAL_STATES = frozenset({"skipped_before_gateway", "call_not_started"})


def identity_is_proven_uncalled(session, action, owner):
    attempts = list(session.scalars(select(ExecutionAttempt).where(
        ExecutionAttempt.action_id == action.id).order_by(ExecutionAttempt.attempt_no).with_for_update()))
    if not attempts or any(not _uncalled_attempt(attempt) for attempt in attempts):
        return False
    if session.scalar(select(GatewayRequestEvidenceJournal.id).where(
            GatewayRequestEvidenceJournal.action_id == action.id).limit(1)) is not None:
        return False
    fences = session.scalars(select(RemoteInvocationFence).where(
        RemoteInvocationFence.action_id == action.id).with_for_update())
    if any(fence.started_at is not None or fence.state not in {"reserved", "terminal"}
            or fence.business_outcome_state not in {"not_called", "safely_not_called"} for fence in fences):
        return False
    if session.scalar(select(FulfillmentRemoteFact.fact_id).where(
            FulfillmentRemoteFact.action_id == action.id,
            FulfillmentRemoteFact.fact_kind != "safely_not_executed").limit(1)) is not None:
        return False
    return session.scalar(select(ViewRemoteFact.id).where(
        ViewRemoteFact.target_peer_id == owner.target_peer_id,
        ViewRemoteFact.channel_message_id == owner.channel_message_id,
        ViewRemoteFact.account_id == owner.account_id,
        ViewRemoteFact.obligation_local_date == owner.obligation_local_date).limit(1)) is None


def _uncalled_attempt(attempt):
    if attempt.gateway_call_started_at is not None or attempt.remote_message_id:
        return False
    result = attempt.result_snapshot or {}
    if result.get("remote_mutation_started") is True or result.get("callback_mutation_started") is True:
        return False
    return (attempt.status in UNCALLED_TERMINAL_STATES
        or (attempt.status == "failed" and result.get("remote_mutation_started") is False))

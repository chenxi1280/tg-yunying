"""Pre-Gateway burst supersession preserves old delivery identities."""
from sqlalchemy import select

from app.models import Action, ContextTurn, ConversationTurnClaim, ExecutionAttempt, GenerationJob
from .generation_provider_lineage import unresolved_generation_lineages


def latest_author_turn(session, event, *, state):
    return session.scalar(select(ContextTurn).where(
        ContextTurn.tenant_id == event.tenant_id,
        ContextTurn.surface == event.surface,
        ContextTurn.canonical_peer_id == event.canonical_peer_id,
        ContextTurn.author_peer_id == event.author_peer_id,
        ContextTurn.state == state,
    ).order_by(ContextTurn.last_event_at.desc(), ContextTurn.id.desc())
        .limit(1).with_for_update())


def supersede_unissued_turn(session, turn, *, invalidate):
    claim = session.scalar(select(ConversationTurnClaim).where(
        ConversationTurnClaim.context_turn_id == turn.id,
    ).with_for_update().execution_options(populate_existing=True))
    if claim is None or claim.state not in {"claimed", "bound"}:
        return False
    if claim.action_id and not _action_is_unissued(session, claim.action_id):
        return False
    invalidate(session, [turn], reason="pre_gateway_late_burst_superseded")
    turn.state = "superseded"
    return True


def _action_is_unissued(session, action_id):
    # Both supersession and the final call-issued transaction lock the claim.
    # Do not lock Action here: dispatcher acquires Action before that claim.
    action = session.scalar(select(Action).where(Action.id == action_id)
        .execution_options(populate_existing=True))
    if action is None or action.status in {"success", "result_unknown"}:
        return False
    job_id = str((action.payload or {}).get("generation_job_id") or "")
    job = session.get(GenerationJob, job_id) if job_id else None
    if job is not None and unresolved_generation_lineages(session, [job]):
        return False
    issued = session.scalar(select(ExecutionAttempt.id).where(
        ExecutionAttempt.action_id == action_id,
        ExecutionAttempt.gateway_call_started_at.is_not(None),
    ).limit(1))
    return issued is None

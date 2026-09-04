"""An album account is confirmed only by every frozen child remote fact."""
from sqlalchemy import func, select

from app.models import Action, AlbumReactionParticipation, ReactionCapacityAllocationEpoch, ReactionFulfillmentObligation, ReactionRemoteFact
from .channel_fulfillment_identity import reaction_state_revision


def album_extra_units(session, task, ledger) -> int:
    return int(session.scalar(select(func.coalesce(func.sum(AlbumReactionParticipation.child_count - 1), 0)).where(
        AlbumReactionParticipation.task_id == task.id,
        AlbumReactionParticipation.lifecycle_epoch == task.task_lifecycle_epoch,
        AlbumReactionParticipation.task_day_ledger_id == ledger.id)) or 0)


def album_child_confirmed(session, parent, child) -> bool:
    return session.scalar(select(ReactionRemoteFact.id).where(
        ReactionRemoteFact.tenant_id == parent.tenant_id,
        ReactionRemoteFact.obligation_id == child["obligation_id"],
        ReactionRemoteFact.target_peer_id == parent.target_peer_id,
        ReactionRemoteFact.channel_message_id == child["message_id"],
        ReactionRemoteFact.account_id == parent.account_id,
        ReactionRemoteFact.reaction_state_revision == reaction_state_revision(child["reaction"]),
    ).limit(1)) is not None


def settle_album_participations(session, obligation):
    session.flush()
    parents = session.scalars(select(AlbumReactionParticipation).where(
        AlbumReactionParticipation.task_id == obligation.task_id,
        AlbumReactionParticipation.account_id == obligation.account_id,
        AlbumReactionParticipation.status != "confirmed").with_for_update())
    for parent in parents:
        if not any(c["obligation_id"] == obligation.id for c in parent.children):
            continue
        count = sum(album_child_confirmed(session, parent, c) for c in parent.children)
        parent.status = "confirmed" if count == parent.child_count else "partial_child_confirmed"


def album_reaction_summary(session, task):
    parents = list(session.scalars(select(AlbumReactionParticipation).where(
        AlbumReactionParticipation.task_id == task.id,
        AlbumReactionParticipation.lifecycle_epoch == task.task_lifecycle_epoch)))
    configured = configured_album_accounts(session, task)
    if not parents and not configured:
        return None
    confirmations = [sum(album_child_confirmed(session, p, c) for c in p.children) for p in parents]
    unknown = sum(album_child_unknown(session, c) for p in parents for c in p.children)
    return {"configured_distinct_accounts": sum(configured.values()) if configured else None,
            "materialized_accounts": len(parents),
            "confirmed_accounts": sum(n == p.child_count for p, n in zip(parents, confirmations)),
            "planned_child_rpc": sum(p.child_count for p in parents),
            "confirmed_child_reactions": sum(confirmations), "unknown_children": unknown,
            "partial_accounts": sum(0 < n < p.child_count for p, n in zip(parents, confirmations))}


def album_child_unknown(session, child):
    obligation = session.get(ReactionFulfillmentObligation, child["obligation_id"])
    action = session.get(Action, obligation.current_action_id) if obligation and obligation.current_action_id else None
    return bool(action and action.status == "unknown_after_send")


def configured_album_accounts(session, task):
    epochs = session.scalars(select(ReactionCapacityAllocationEpoch).where(
        ReactionCapacityAllocationEpoch.task_id == task.id,
        ReactionCapacityAllocationEpoch.task_lifecycle_epoch == task.task_lifecycle_epoch,
        ReactionCapacityAllocationEpoch.state == "active").order_by(ReactionCapacityAllocationEpoch.created_at))
    return {str(d["album_id"]): int(d["required_count"]) for epoch in epochs
            for d in epoch.source_demands if d.get("album_id")}


def album_targets_confirmed(session, task, *, album_ids):
    targets = configured_album_accounts(session, task)
    if any(album_id not in targets for album_id in album_ids):
        return False
    parents = session.scalars(select(AlbumReactionParticipation).where(
        AlbumReactionParticipation.task_id == task.id,
        AlbumReactionParticipation.lifecycle_epoch == task.task_lifecycle_epoch,
        AlbumReactionParticipation.album_id.in_(album_ids)))
    confirmed = {album_id: set() for album_id in album_ids}
    for parent in parents:
        if len(parent.children) == parent.child_count and all(album_child_confirmed(session, parent, child) for child in parent.children):
            confirmed[parent.album_id].add(parent.account_id)
    return all(len(confirmed[album_id]) >= targets[album_id] for album_id in album_ids)

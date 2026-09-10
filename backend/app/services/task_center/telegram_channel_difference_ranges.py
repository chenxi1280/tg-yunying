from __future__ import annotations

from sqlalchemy import select

from app.models import Task
from app.models.group_clone import CloneSourceStreamState
from app.models.telegram_updates import (
    TelegramAuthorizationUpdateEvent as UpdateEvent,
    TelegramAuthorizationUpdateSubscription as Subscription,
)

from .telegram_update_ingress import NormalizedUpdateIngress, ingest_normalized_update

CHANNEL_DIFFERENCE_RANGE = "ChannelDifferenceRange"


def record_channel_difference_range(session, state, batch, *, claim, peer_id, requested_pts):
    if batch.scope != "channel" or batch.status == "too_long":
        return
    response_pts = int(batch.cursor.get("pts") or 0)
    if not peer_id or requested_pts is None or requested_pts <= 0 or response_pts < requested_pts:
        raise ValueError("telegram_channel_difference_range_invalid")
    ingress = NormalizedUpdateIngress(
        update_identity_key=f"channel_difference_range:{peer_id}:{requested_pts}:{response_pts}",
        constructor_name=CHANNEL_DIFFERENCE_RANGE,
        pts_evidence=response_pts,
        pts_count_evidence=response_pts - requested_pts,
        routing_peer_type="channel",
        routing_peer_id=str(peer_id),
        normalized_items=(),
        cursor_scope="event_only",
    )
    ingest_normalized_update(
        session, state.id, ingress,
        owner_id=claim.owner_id, owner_fencing_epoch=claim.fencing_epoch,
    )


def channel_difference_covers(session, stream, envelope) -> bool:
    if (
        envelope.authorization_update_state_id != stream.authorization_update_state_id
        or envelope.routing_peer_type != stream.source_peer_type
        or envelope.routing_peer_id != stream.source_peer_id
    ):
        return False
    proof = select(UpdateEvent.id).join(
        Subscription,
        Subscription.authorization_update_state_id == UpdateEvent.authorization_update_state_id,
    ).join(Task, Task.id == Subscription.task_id).where(
        UpdateEvent.authorization_update_state_id == stream.authorization_update_state_id,
        UpdateEvent.constructor_name == CHANNEL_DIFFERENCE_RANGE,
        UpdateEvent.routing_peer_type == stream.source_peer_type,
        UpdateEvent.routing_peer_id == stream.source_peer_id,
        UpdateEvent.pts_count_evidence > 0,
        UpdateEvent.pts_evidence - UpdateEvent.pts_count_evidence <= stream.channel_pts,
        UpdateEvent.pts_evidence >= envelope.pts_evidence,
        UpdateEvent.ingress_order_no > Subscription.start_ingress_order,
        Subscription.task_id == stream.task_id,
        Subscription.task_epoch == stream.task_lifecycle_epoch,
        Subscription.task_epoch == Task.task_lifecycle_epoch,
        Subscription.source_peer_type == stream.source_peer_type,
        Subscription.source_peer_id == stream.source_peer_id,
        Subscription.state == "active",
    ).limit(1)
    return session.scalar(proof) is not None


def uncovered_channel_gaps(session, state_id) -> dict[str, int]:
    rows = session.execute(select(Task, CloneSourceStreamState).join(
        CloneSourceStreamState, CloneSourceStreamState.task_id == Task.id,
    ).where(
        CloneSourceStreamState.authorization_update_state_id == state_id,
        CloneSourceStreamState.task_lifecycle_epoch == Task.task_lifecycle_epoch,
        CloneSourceStreamState.state.in_(("gap", "catching_up", "live")),
        CloneSourceStreamState.channel_pts > 0,
        Task.type == "group_clone", Task.status.in_(("pending", "running", "failed")),
        Task.deleted_at.is_(None), Task.retired_at.is_(None),
    )).all()
    gaps = {}
    for task, stream in rows:
        gap_order = int((task.stats or {}).get("clone_gap_at") or 0)
        if gap_order <= stream.last_consumed_ingress_order_no:
            continue
        head = session.scalar(select(UpdateEvent).where(
            UpdateEvent.authorization_update_state_id == state_id,
            UpdateEvent.ingress_order_no == gap_order,
        ))
        if head is None or channel_difference_covers(session, stream, head):
            continue
        peer_id = stream.source_peer_id
        gaps[peer_id] = min(gaps.get(peer_id, stream.channel_pts), stream.channel_pts)
    return gaps

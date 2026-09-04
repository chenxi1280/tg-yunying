"""Fence late source-page results after another listener has taken ownership."""
from sqlalchemy import select

from app.models import ListenerSourceState


class ChannelSourceClaimLost(RuntimeError):
    pass


def locked_source_state(session, source, state_id):
    state = session.scalar(select(ListenerSourceState).where(ListenerSourceState.id == state_id)
        .with_for_update(skip_locked=True).execution_options(populate_existing=True))
    if state is None:
        raise ChannelSourceClaimLost("channel_listener_state_claim_lost")
    revision = getattr(source, "claimed_revision", None)
    if revision is not None and (state.lease_owner != source.claim_owner or state.snapshot_revision != revision):
        raise ChannelSourceClaimLost("channel_listener_state_claim_lost")
    return state

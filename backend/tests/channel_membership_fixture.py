"""Explicit joined-account prerequisites for downstream channel tests."""
from app.services.task_center.channel_membership import mark_channel_membership_joined


def seed_joined_channel(session, channel_id, account_ids, *, tenant_id=1):
    session.flush()
    for account_id in account_ids:
        mark_channel_membership_joined(session, tenant_id, channel_id, account_id)
    session.flush()

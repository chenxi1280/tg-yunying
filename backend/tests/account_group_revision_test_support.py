"""Explicit initial evidence for tests that construct accounts through ORM fixtures."""
from app.services.account_group_revisions import (
    begin_membership_change, finish_membership_change, initialize_group_revisions,
)


def bootstrap_groups(session, tenant_id, pool_ids):
    session.flush()
    return initialize_group_revisions(session, tenant_id, pool_ids,
        actor="test_fixture", reason="explicit_fixture_bootstrap")


def set_account_status(session, account, *, status):
    token = begin_membership_change(session, account.tenant_id,
        (account.pool_id,) if account.pool_id is not None else (),
        actor="test_fixture", reason="explicit_fixture_status")
    account.status = status
    finish_membership_change(session, token)

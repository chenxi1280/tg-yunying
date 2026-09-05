"""Explicit initial evidence for tests that construct accounts through ORM fixtures."""
from app.services.account_group_revisions import initialize_group_revisions


def bootstrap_groups(session, tenant_id, pool_ids):
    session.flush()
    return initialize_group_revisions(session, tenant_id, pool_ids,
        actor="test_fixture", reason="explicit_fixture_bootstrap")

"""Bounded canonical reads shared by membership writers and plan freezing."""
from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import and_, func, inspect, select
from sqlalchemy.orm.attributes import set_committed_value

from app.models import (
    AccountGroupMembershipRevision, AccountGroupStateRevision, AccountPool,
    AccountStatus, Tenant, TgAccount,
)
from app.common.state_hash import canonical_state_hash


POOL_STATE_ATTRIBUTES = ("tenant_id", "pool_purpose", "system_key", "is_system", "is_enabled")
ACCOUNT_MEMBERSHIP_ATTRIBUTES = ("tenant_id", "pool_id", "account_identity", "deleted_at")


@dataclass(frozen=True)
class GroupRevisionPair:
    pool_id: int
    membership: AccountGroupMembershipRevision | None
    state: AccountGroupStateRevision | None

    @property
    def versions(self):
        return (self.membership.revision if self.membership else 0,
            self.state.revision if self.state else 0)


def lock_membership_tenant(session, tenant_id):
    # Publish only a new parent; unrelated pending membership edits stay unflushed.
    pending = next((row for row in session.new if isinstance(row, Tenant) and row.id == tenant_id), None)
    if pending is not None:
        session.flush([pending])
    with session.no_autoflush:
        # Serialize writers without conflicting with new Task foreign-key locks.
        found = session.scalar(select(Tenant.id).where(Tenant.id == tenant_id)
            .with_for_update(key_share=True))
    if found is None:
        raise ValueError("membership_tenant_not_found")


def lock_membership_account(session, account_id):
    with session.no_autoflush:
        tenant_id = session.scalar(select(TgAccount.tenant_id).where(TgAccount.id == account_id))
    if tenant_id is None:
        raise ValueError("account not found")
    lock_membership_tenant(session, tenant_id)
    with session.no_autoflush:
        account = session.scalar(select(TgAccount).where(TgAccount.id == account_id).with_for_update())
        _assert_membership_unmodified(account, ACCOUNT_MEMBERSHIP_ATTRIBUTES)
        session.refresh(account, attribute_names=list(ACCOUNT_MEMBERSHIP_ATTRIBUTES))
    if account.tenant_id != tenant_id:
        raise ValueError("membership_account_tenant_changed")
    return account


def locked_membership_pools(session, tenant_id, pool_ids):
    with session.no_autoflush:
        rows = tuple(session.execute(select(AccountPool,
            *(getattr(AccountPool, name) for name in POOL_STATE_ATTRIBUTES)).where(
            AccountPool.tenant_id == tenant_id, AccountPool.id.in_(pool_ids))
            .order_by(AccountPool.id).with_for_update()))
    pools = tuple(row[0] for row in rows)
    if tuple(pool.id for pool in pools) != tuple(pool_ids):
        raise ValueError("membership_pool_not_found_or_cross_tenant")
    for row in rows:
        _assert_membership_unmodified(row[0], POOL_STATE_ATTRIBUTES)
        for name, value in zip(POOL_STATE_ATTRIBUTES, row[1:]):
            set_committed_value(row[0], name, value)
    return pools


def _assert_membership_unmodified(row, names):
    if any(inspect(row).attrs[name].history.has_changes() for name in names):
        raise ValueError("membership_change_started_after_mutation")


def read_group_revisions_for_snapshot(session, tenant_id, pool_ids):
    ids = tuple(sorted(set(pool_ids)))
    lock_membership_tenant(session, tenant_id)
    pools = locked_membership_pools(session, tenant_id, ids)
    pairs = current_group_revisions(session, tenant_id, ids)
    members = current_member_contracts(session, tenant_id, ids)
    for pair, pool in zip(pairs, pools):
        assert_group_revision_matches(pair, members[pair.pool_id], group_state_snapshot(pool))
    return pairs


def current_group_revisions(session, tenant_id, pool_ids):
    memberships = _current_rows(session, AccountGroupMembershipRevision,
        tenant_id=tenant_id, pool_ids=pool_ids)
    states = _current_rows(session, AccountGroupStateRevision,
        tenant_id=tenant_id, pool_ids=pool_ids)
    return tuple(GroupRevisionPair(pool_id, memberships.get(pool_id), states.get(pool_id))
        for pool_id in pool_ids)


def _current_rows(session, model, *, tenant_id, pool_ids):
    latest = select(model.account_pool_id, func.max(model.revision).label("revision")).where(
        model.tenant_id == tenant_id, model.account_pool_id.in_(pool_ids)
        ).group_by(model.account_pool_id).subquery()
    rows = session.scalars(select(model).join(latest, and_(
        model.account_pool_id == latest.c.account_pool_id, model.revision == latest.c.revision))
        .where(model.tenant_id == tenant_id))
    return {row.account_pool_id: row for row in rows}


def current_member_contracts(session, tenant_id, pool_ids):
    rows = session.execute(select(TgAccount.id, TgAccount.pool_id, TgAccount.account_identity,
        TgAccount.status, TgAccount.account_lifecycle_status).where(
            TgAccount.tenant_id == tenant_id, TgAccount.pool_id.in_(pool_ids),
            TgAccount.deleted_at.is_(None)).order_by(TgAccount.id))
    members = defaultdict(list)
    for account_id, pool_id, identity, status, lifecycle in rows:
        members[pool_id].append({"account_id": account_id, "account_identity": identity,
            "enabled": status != AccountStatus.DISABLED.value, "lifecycle": lifecycle})
    return {pool_id: members[pool_id] for pool_id in pool_ids}


def group_state_snapshot(pool):
    return {"tenant_id": pool.tenant_id, "pool_id": pool.id,
        "pool_purpose": pool.pool_purpose, "system_key": pool.system_key,
        "is_system": pool.is_system, "is_enabled": pool.is_enabled, "deleted": False}


def assert_group_revision_matches(pair, members, state):
    if pair.membership is None or pair.state is None:
        raise ValueError("account_group_revision_missing")
    ids = [item["account_id"] for item in members]
    membership = pair.membership
    if (membership.member_account_ids != ids or membership.member_contracts != members
            or membership.member_set_hash != canonical_state_hash(ids)
            or membership.membership_hash != canonical_state_hash(members)
            or pair.state.group_state != state or pair.state.state_hash != canonical_state_hash(state)):
        raise ValueError("account_group_revision_drift")

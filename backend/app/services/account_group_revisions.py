"""Commit membership/state successors and wake events in the original transaction."""
from dataclasses import dataclass

from sqlalchemy import select

from app.models import AccountGroupMembershipRevision, AccountGroupStateRevision, AccountPool, StageWakeOutbox
from ._common import _now
from .account_group_revision_snapshot import (
    GroupRevisionPair, assert_group_revision_matches, current_group_revisions,
    current_member_contracts, group_state_snapshot, lock_membership_tenant, locked_membership_pools,
)
from app.common.state_hash import canonical_state_hash


MEMBERSHIP_WAKE_STAGE = "refresh_engagement_membership"


@dataclass(frozen=True)
class MembershipChange:
    tenant_id: int
    pool_ids: tuple[int, ...]
    before: tuple[GroupRevisionPair, ...]
    actor: str
    reason: str
    expected_state_hash: str


def begin_membership_change(session, tenant_id, pool_ids, *, actor, reason,
        expected_versions=None, baseline_reason="mutation_baseline"):
    ids = tuple(sorted(set(pool_ids)))
    lock_membership_tenant(session, tenant_id)
    pools = locked_membership_pools(session, tenant_id, ids)
    before = current_group_revisions(session, tenant_id, ids)
    if expected_versions is not None and {
            pair.pool_id: pair.versions for pair in before} != expected_versions:
        raise ValueError("account_group_revision_conflict")
    members = current_member_contracts(session, tenant_id, ids)
    before = tuple(_ensure_baseline(session, pair, members[pair.pool_id],
        state=group_state_snapshot(pool), actor=actor, reason=baseline_reason)
        for pair, pool in zip(before, pools))
    session.flush()
    return MembershipChange(tenant_id, ids, before, actor, reason,
        canonical_state_hash([_pair_identity(pair) for pair in before]))


def initialize_group_revisions(session, tenant_id, pool_ids, *, actor, reason, expected_versions=None):
    token = begin_membership_change(session, tenant_id, pool_ids, actor=actor,
        reason=reason, expected_versions=expected_versions, baseline_reason=reason)
    return finish_membership_change(session, token)


def _ensure_baseline(session, pair, members, *, state, actor, reason):
    if pair.membership is None and pair.state is None:
        return _append_revisions(session, pair, members, state=state, actor=actor, reason=reason)
    assert_group_revision_matches(pair, members, state)
    return pair


def finish_membership_change(session, token):
    session.flush()
    current = current_group_revisions(session, token.tenant_id, token.pool_ids)
    if token.expected_state_hash != canonical_state_hash([_pair_identity(pair) for pair in current]):
        raise ValueError("account_group_revision_conflict")
    pools = {pool.id: pool for pool in session.scalars(select(AccountPool).where(
        AccountPool.tenant_id == token.tenant_id, AccountPool.id.in_(token.pool_ids)))}
    members = current_member_contracts(session, token.tenant_id, token.pool_ids)
    result = tuple(_append_revisions(session, pair, members[pair.pool_id],
        state=group_state_snapshot(pools[pair.pool_id]) if pair.pool_id in pools
            else {**pair.state.group_state, "deleted": True},
        actor=token.actor, reason=token.reason) for pair in token.before)
    session.flush()
    return result


def _pair_identity(pair):
    membership, state = pair.membership, pair.state
    return (membership.id, membership.revision, membership.membership_hash,
        membership.member_set_hash, membership.member_account_ids, membership.member_contracts,
        state.id, state.revision, state.state_hash, state.group_state)


def _append_revisions(session, pair, members, *, state, actor, reason):
    membership, group_state = pair.membership, pair.state
    if membership is None or membership.membership_hash != canonical_state_hash(members):
        membership = _membership_successor(pair, members, state, actor=actor, reason=reason)
        session.add(membership)
        session.flush()
        _add_wake(session, membership, "account_group_membership")
    if group_state is None or group_state.state_hash != canonical_state_hash(state):
        group_state = _state_successor(pair, state, actor=actor, reason=reason)
        session.add(group_state)
        session.flush()
        _add_wake(session, group_state, "account_group_state")
    return GroupRevisionPair(pair.pool_id, membership, group_state)


def _membership_successor(pair, members, state, *, actor, reason):
    ids = [item["account_id"] for item in members]
    return AccountGroupMembershipRevision(tenant_id=state["tenant_id"], account_pool_id=pair.pool_id,
        revision=pair.versions[0] + 1, member_account_ids=ids, member_contracts=members,
        member_set_hash=canonical_state_hash(ids), membership_hash=canonical_state_hash(members),
        supersedes_revision_id=pair.membership.id if pair.membership else None,
        actor=actor, reason=reason)


def _state_successor(pair, state, *, actor, reason):
    return AccountGroupStateRevision(tenant_id=state["tenant_id"], account_pool_id=pair.pool_id,
        revision=pair.versions[1] + 1, group_state=state, state_hash=canonical_state_hash(state),
        supersedes_revision_id=pair.state.id if pair.state else None, actor=actor, reason=reason)


def _add_wake(session, revision, aggregate_type):
    session.add(StageWakeOutbox(tenant_id=revision.tenant_id, aggregate_type=aggregate_type,
        aggregate_id=revision.id, aggregate_revision=revision.revision,
        stage=MEMBERSHIP_WAKE_STAGE, available_at=_now()))

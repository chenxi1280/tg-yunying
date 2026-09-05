"""Explicit whole-tenant preview and compare-and-swap initial membership capture."""
from sqlalchemy import select

from app.common.state_hash import canonical_state_hash
from app.models import AccountPool, Tenant
from ._common import audit
from .account_group_revision_snapshot import (
    assert_group_revision_matches, current_group_revisions, current_member_contracts,
    group_state_snapshot, lock_membership_tenant, locked_membership_pools,
)
from .account_group_revisions import initialize_group_revisions


BOOTSTRAP_SCHEMA_VERSION = 1
AUDIT_REFERENCE_MAX_LENGTH = 100
ACTOR_MAX_LENGTH = 100


def preview_group_revisions(session, tenant_id):
    with session.no_autoflush:
        if session.scalar(select(Tenant.id).where(Tenant.id == tenant_id)) is None:
            raise ValueError("membership_tenant_not_found")
        pools = _pool_rows(session, tenant_id)
        ids = tuple(pool.id for pool in pools)
        pairs = current_group_revisions(session, tenant_id, ids)
        members = current_member_contracts(session, tenant_id, ids)
        state = {"tenant_id": tenant_id, "groups": [_group_preview(pool, pair, members[pool.id])
            for pool, pair in zip(pools, pairs)]}
    return {"schema_version": BOOTSTRAP_SCHEMA_VERSION, "state": state,
        "state_hash": canonical_state_hash(state)}


def _pool_rows(session, tenant_id):
    return tuple(session.execute(select(AccountPool.id, AccountPool.tenant_id, AccountPool.name,
        AccountPool.pool_purpose, AccountPool.system_key, AccountPool.is_system, AccountPool.is_enabled)
        .where(AccountPool.tenant_id == tenant_id).order_by(AccountPool.id)))


def _group_preview(pool, pair, members):
    state = group_state_snapshot(pool)
    issue = None
    if pair.membership is not None or pair.state is not None:
        try:
            assert_group_revision_matches(pair, members, state)
        except ValueError as exc:
            issue = str(exc)
    return {"pool_id": pool.id, "name": pool.name, "state": state,
        "member_contracts": members, "member_set_hash": canonical_state_hash([m["account_id"] for m in members]),
        "membership_hash": canonical_state_hash(members), "group_state_hash": canonical_state_hash(state),
        "membership_revision": pair.versions[0], "group_state_revision": pair.versions[1],
        "membership_revision_id": pair.membership.id if pair.membership else None,
        "group_state_revision_id": pair.state.id if pair.state else None, "issue": issue}


def apply_group_revision_bootstrap(session, preview, *, actor, audit_reference):
    _validate_request(preview, actor, audit_reference=audit_reference)
    tenant_id = preview["state"]["tenant_id"]
    lock_membership_tenant(session, tenant_id)
    ids = tuple(pool.id for pool in _pool_rows(session, tenant_id))
    locked_membership_pools(session, tenant_id, ids)
    before = preview_group_revisions(session, tenant_id)
    if before["state_hash"] != preview["state_hash"]:
        raise ValueError("account_group_bootstrap_preview_conflict")
    groups = before["state"]["groups"]
    if any(group["issue"] for group in groups):
        raise ValueError("account_group_bootstrap_requires_drift_repair")
    expected = {group["pool_id"]: (group["membership_revision"], group["group_state_revision"])
        for group in groups}
    initialize_group_revisions(session, tenant_id, ids, actor=actor,
        reason=f"initial_bootstrap:{audit_reference}", expected_versions=expected)
    after = preview_group_revisions(session, tenant_id)
    created = sum(group["membership_revision"] == 0 for group in groups)
    receipt = {"tenant_id": tenant_id, "audit_reference": audit_reference,
        "before_hash": before["state_hash"], "after_hash": after["state_hash"],
        "initialized_group_count": created, "group_count": len(groups), "after": after}
    audit(session, tenant_id=tenant_id, actor=actor, action="初始化账号组成员版本",
        target_type="account_group_membership", target_id=str(tenant_id),
        detail=f"audit={audit_reference};groups={len(groups)};initialized={created};after={after['state_hash']}")
    session.flush()
    return receipt


def _validate_request(preview, actor, *, audit_reference):
    if not actor or len(actor) > ACTOR_MAX_LENGTH:
        raise ValueError("account_group_bootstrap_actor_required")
    if not audit_reference or len(audit_reference) > AUDIT_REFERENCE_MAX_LENGTH:
        raise ValueError("account_group_bootstrap_audit_reference_required")
    if (preview.get("schema_version") != BOOTSTRAP_SCHEMA_VERSION
            or type(preview.get("state", {}).get("tenant_id")) is not int
            or preview["state"]["tenant_id"] <= 0
            or canonical_state_hash(preview["state"]) != preview.get("state_hash")):
        raise ValueError("account_group_bootstrap_preview_invalid")

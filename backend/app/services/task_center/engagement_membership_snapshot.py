"""Freeze proven group revisions without reconstructing missing membership evidence."""
from copy import deepcopy

from app.services.account_group_revision_snapshot import read_group_revisions_for_snapshot


def capture_membership_groups(session, tenant_id, group_ids):
    pairs = read_group_revisions_for_snapshot(session, tenant_id, group_ids)
    return [_group_snapshot(pair) for pair in pairs]


def _group_snapshot(pair):
    membership, state = pair.membership, pair.state
    contract = state.group_state
    if (str(contract["pool_purpose"] or "normal") != "normal"
            or str(contract["system_key"] or "") not in {"", "normal"} or contract["deleted"]):
        raise ValueError(f"account_group_purpose_mismatch:{pair.pool_id}")
    if any(member["account_identity"] != "normal" for member in membership.member_contracts):
        raise ValueError(f"account_group_member_purpose_mismatch:{pair.pool_id}")
    return {
        "group_id": pair.pool_id,
        "membership_revision_id": membership.id,
        "membership_revision": membership.revision,
        "member_account_ids": list(membership.member_account_ids),
        "member_contracts": deepcopy(membership.member_contracts),
        "member_set_hash": membership.member_set_hash,
        "membership_hash": membership.membership_hash,
        "group_state_revision_id": state.id,
        "group_state_revision": state.revision,
        "group_state_hash": state.state_hash,
        "group_state": deepcopy(contract),
    }

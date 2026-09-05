"""Stable operational participation; transient readiness belongs to admission."""
from sqlalchemy.orm import Session

from app.models import Task, Tenant


def policy_eligible_member_ids(session: Session, task: Task, snapshot) -> tuple[int, ...]:
    contracts = _frozen_contracts(snapshot)
    tenant = session.get(Tenant, task.tenant_id)
    excluded = {int(tenant.group_rescue_admin_account_id or 0)} if tenant else set()
    if task.type == "group_ai_chat":
        excluded.add(int((task.type_config or {}).get("group_rescue_admin_account_id") or 0))
    return tuple(account_id for account_id in snapshot.member_account_ids
        if account_id not in excluded and contracts[account_id]["enabled"] is True
        and contracts[account_id]["lifecycle"] == "business_active")


def _frozen_contracts(snapshot):
    if any("member_contracts" not in group for group in snapshot.group_memberships):
        raise ValueError("account_group_snapshot_membership_unproven")
    members = [member for group in snapshot.group_memberships for member in group["member_contracts"]]
    contracts = {member["account_id"]: member for member in members}
    if len(contracts) != len(members) or sorted(contracts) != sorted(snapshot.member_account_ids):
        raise ValueError("account_group_snapshot_membership_invalid")
    return contracts

"""Stable operational participation; transient readiness belongs to admission."""
from sqlalchemy.orm import Session

from app.models import Task, Tenant


def policy_eligible_member_ids(session: Session, task: Task, member_ids) -> tuple[int, ...]:
    tenant = session.get(Tenant, task.tenant_id)
    excluded = {int(tenant.group_rescue_admin_account_id or 0)} if tenant else set()
    if task.type == "group_ai_chat":
        excluded.add(int((task.type_config or {}).get("group_rescue_admin_account_id") or 0))
    return tuple(int(item) for item in member_ids if int(item) not in excluded)

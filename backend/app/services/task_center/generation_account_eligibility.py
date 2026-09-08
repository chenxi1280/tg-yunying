"""Recheck the assigned account at the durable Provider call-start boundary."""
from sqlalchemy import select

from app.models import Action, Task
from .account_assignment_eligibility import UNIFIED_CONTRACT, require_action_assignment_account


def require_generation_accounts(session, jobs):
    for job in jobs:
        task = session.get(Task, job.task_id)
        if (task.type_config or {}).get("engagement_contract_version") != UNIFIED_CONTRACT:
            continue
        actions = list(session.scalars(select(Action).where(
            Action.tenant_id == job.tenant_id, Action.task_id == job.task_id,
            Action.task_lifecycle_epoch == job.task_lifecycle_epoch,
            Action.status.in_(("pending", "executing")),
            Action.payload["generation_job_id"].as_string() == job.id,
        )))
        if len(actions) != 1:
            raise ValueError("generation_account_assignment_unproven")
        require_action_assignment_account(session, actions[0])

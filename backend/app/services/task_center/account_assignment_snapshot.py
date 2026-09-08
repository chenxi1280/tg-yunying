"""Persist the qualification basis alongside the immutable participation plan."""
import hashlib
import json

from sqlalchemy import and_, select
from app.models import PlanningAdmissionSnapshot, TgAccount, TgAccountAuthorization, TgAccountOnlineState
from app.services._common import _now


POLICY = "account_assignment_eligibility_v1"


def record_assignment_snapshot(session, task, plan):
    summary = dict((task.stats or {}).get("account_assignment_eligibility") or {})
    reasons = summary.get("excluded_accounts") or {}
    ids = sorted(set(plan.policy_eligible_account_ids or []) | set(map(int, reasons)))
    fields = (TgAccount.id, TgAccount.current_authorization_id, TgAccount.authorization_generation,
        TgAccount.authorization_fact_generation, TgAccount.connection_generation,
        TgAccount.telegram_freeze_observed_at, TgAccount.status, TgAccount.telegram_frozen,
        TgAccountAuthorization.slot_generation, TgAccountAuthorization.fact_version,
        TgAccountAuthorization.health_status, TgAccountAuthorization.remote_authorization_state,
        TgAccountOnlineState.online_status, TgAccountOnlineState.failure_type,
        TgAccountOnlineState.session_id, TgAccountOnlineState.last_probe_at)
    rows = session.execute(select(*fields).outerjoin(TgAccountAuthorization,
        TgAccountAuthorization.id == TgAccount.current_authorization_id).outerjoin(TgAccountOnlineState, and_(
            TgAccountOnlineState.tenant_id == TgAccount.tenant_id, TgAccountOnlineState.account_id == TgAccount.id)).where(
            TgAccount.tenant_id == task.tenant_id, TgAccount.id.in_(ids)).order_by(TgAccount.id))
    paths = [{**dict(row._mapping), "reason": reasons.get(str(row.id), ""), "policy": POLICY} for row in rows]
    paths = json.loads(json.dumps(paths, default=str))
    digest = hashlib.sha256(json.dumps(paths, sort_keys=True).encode()).hexdigest()
    session.add(PlanningAdmissionSnapshot(tenant_id=task.tenant_id, task_id=task.id,
        task_lifecycle_epoch=task.task_lifecycle_epoch, participation_plan_id=plan.id,
        participation_unit=plan.participation_unit, planning_horizon=POLICY,
        dependency_revision_set_hash=digest, account_paths=paths,
        admissible_account_ids=list(plan.policy_eligible_account_ids), deficit_account_ids=list(map(int, reasons)),
        decision="eligible" if plan.policy_eligible_account_ids else "no_eligible_accounts",
        decision_hash=digest, created_at=_now()))

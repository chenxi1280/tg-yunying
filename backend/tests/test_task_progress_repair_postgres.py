from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.models import (
    AccountPacingReservation, AccountPortfolioLoadReservation, Action, AuditLog, OperationTarget, PortfolioFeasibilityPlanRevision,
    Task, TaskDayLedger, TaskMembershipAdmissionItem, Tenant, TgAccount, TgGroup,
)
from app.services._common import _now
from app.services.task_center.executors.group_ai_extra_candidates import (
    DailyGroupExtraCandidateSpec, daily_group_extra_candidate_ids,
)
from tests.test_group_ai_extra_candidates import _add_candidate
from tests.test_group_ai_extra_portfolio import _pending
from tests import test_runtime_retention_protection_postgres as postgres_support
from tests.test_reaction_backlog_replan import OPERATION, SCOPE, seed_backlog
from app.services.task_center.reaction_backlog_replan import apply_reaction_backlog, verify_reaction_backlog
from app.services.task_center.reaction_backlog_snapshot import preview_reaction_backlog


pytestmark = [pytest.mark.isolated_postgres, pytest.mark.allow_missing_rule_binding]
database = postgres_support.database


def _seed_extra(session):
    now = _now()
    session.add(Tenant(id=1, name="QA extra"))
    session.flush()
    task = Task(id="task", tenant_id=1, name="QA", type="group_ai_chat", status="running")
    group = TgGroup(id=1, tenant_id=1, tg_peer_id="-1001", title="QA")
    target = OperationTarget(id=1, tenant_id=1, target_type="group", tg_peer_id="-1001", title="QA")
    session.add_all([task, group, target])
    session.flush()
    session.add(TaskDayLedger(id="ledger", tenant_id=1, task_id=task.id, timezone_snapshot="Asia/Shanghai",
        timezone_revision=1, obligation_local_date=now.date(), period_start_at=now,
        deadline_at=now + timedelta(days=1), day_phase="full_day", planning_anchor_at=now))
    session.add(PortfolioFeasibilityPlanRevision(id="plan", tenant_id=1, planning_horizon=str(now.date()),
        plan_revision=1, task_set_hash="QA", trigger_task_id=task.id, trigger_kind="authored_message",
        trigger_identity="QA", input_hash="QA", decision="guaranteed_achievable"))
    session.flush()
    for account_id in (1, 2):
        _add_candidate(session, task=task, target=target, ledger_id="ledger", coverage_date=now.date(),
                       group=group, account_id=account_id)
    session.flush([row for row in session.new if isinstance(row, TgAccount)])
    session.flush([row for row in session.new if isinstance(row, TaskMembershipAdmissionItem)])
    session.flush()
    for account_id in (1, 2):
        session.add(AccountPortfolioLoadReservation(tenant_id=1, task_id=task.id, task_day_ledger_id="ledger",
            portfolio_plan_id="plan", account_id=account_id, task_day=now.date(), action_class="authored_message",
            demand_identity="QA", demand_hash="QA", reserved_units=1))
    session.flush()
    return DailyGroupExtraCandidateSpec(1, task.id, group.id, "ledger", now.date())


def test_postgres_extra_query_counts_original_slot_and_observes_committed_work(database):
    with Session(database, autoflush=False) as seed:
        spec = _seed_extra(seed)
        _pending(seed, spec, slot=True)
        assert daily_group_extra_candidate_ids(seed, spec) == [2]
        seed.commit()
    with Session(database) as next_planner:
        assert daily_group_extra_candidate_ids(next_planner, spec) == [2]
        next_planner.get(Action, "pending").status = "skipped"
        assert set(daily_group_extra_candidate_ids(next_planner, spec)) == {1, 2}


def test_postgres_replan_lock_conflict_is_atomic_and_retry_is_idempotent(database):
    with Session(database) as seed:
        seed_backlog(seed)
        preview = preview_reaction_backlog(seed, SCOPE)
    with Session(database) as owner, Session(database) as repair:
        owner.scalar(select(Action).where(Action.id == "old-action").with_for_update())
        with pytest.raises(DBAPIError) as error:
            apply_reaction_backlog(repair, preview, OPERATION)
        assert error.value.orig.sqlstate == "55P03"
        repair.rollback()
        assert repair.get(AccountPacingReservation, "reservation").state == "cancelled"
        assert repair.scalar(select(func.count()).select_from(AuditLog)) == 0
        repair.rollback()
        owner.rollback()
        receipt = apply_reaction_backlog(repair, preview, OPERATION)
        repair.commit()
    with Session(database) as readback:
        assert verify_reaction_backlog(readback, receipt)["persistence_status"] == "persisted_verified"
        assert apply_reaction_backlog(readback, preview, OPERATION) == receipt
        assert readback.scalar(select(func.count()).select_from(AuditLog)) == 1


def test_postgres_replan_rejects_committed_owner_change(database):
    with Session(database) as seed:
        seed_backlog(seed)
        preview = preview_reaction_backlog(seed, SCOPE)
    with Session(database) as worker:
        worker.get(Action, "old-action").claim_owner = "new-worker"
        worker.commit()
    with Session(database) as repair:
        with pytest.raises(ValueError, match="action_owned"):
            apply_reaction_backlog(repair, preview, OPERATION)
        repair.rollback()
        assert repair.get(Action, "old-action").claim_owner == "new-worker"
        assert repair.get(AccountPacingReservation, "reservation").state == "cancelled"

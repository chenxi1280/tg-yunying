from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError
import pytest

from app.database import SessionLocal
from app.models import PlanningAdmissionEvidence, PlanningAdmissionSnapshot, Task, Tenant
from app.services.task_center.planning_admission_evidence import shared_admission_evidence
from app.services.task_center.admission_evidence_maintenance import (
    apply_admission_evidence, preview_admission_evidence, readback_admission_evidence,
)
from tests.test_planning_admission_evidence import CONTEXT, PATHS


TENANT_ID = 926_910


def test_concurrent_planners_share_one_committed_evidence():
    with SessionLocal.begin() as session:
        session.add(Tenant(id=TENANT_ID, name="admission-evidence-test"))
    barrier = Barrier(2, timeout=5)
    def write_paths(_):
        with SessionLocal.begin() as session:
            barrier.wait()
            result = shared_admission_evidence(session, TENANT_ID, [{"account_id": 1, "ready": True}])
            return result.digest
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            digests = list(pool.map(write_paths, range(2)))
        assert digests[0] == digests[1]
        with SessionLocal() as session:
            count = session.scalar(select(func.count()).select_from(PlanningAdmissionEvidence).where(
                PlanningAdmissionEvidence.tenant_id == TENANT_ID))
            assert count == 1
    finally:
        with SessionLocal.begin() as session:
            session.execute(delete(Tenant).where(Tenant.id == TENANT_ID))


def _seed_historical_snapshot():
    from app.models import (Task, TaskAccountGroupBindingSetRevision,
                            AccountGroupMembershipSnapshotSet, TaskParticipationUnitPlan)
    with SessionLocal() as session:
        session.add(Tenant(id=1, name="historical-evidence-test"))
        session.flush()
        task = Task(tenant_id=1, type="channel_view", name="test", status="paused")
        session.add(task)
        session.flush()
        binding = TaskAccountGroupBindingSetRevision(tenant_id=1, task_id=task.id, binding_set_hash="test")
        session.add(binding)
        session.flush()
        membership = AccountGroupMembershipSnapshotSet(tenant_id=1, task_id=task.id,
            binding_set_revision_id=binding.id, participation_unit="test", member_union_hash="test")
        session.add(membership)
        session.flush()
        plan = TaskParticipationUnitPlan(tenant_id=1, task_id=task.id, membership_snapshot_set_id=membership.id,
            participation_kind="test", participation_unit="test", policy_revision="test",
            selection_seed="test", selection_hash="test")
        session.add(plan)
        session.flush()
        row = PlanningAdmissionSnapshot(tenant_id=1, task_id=task.id, task_lifecycle_epoch=task.task_lifecycle_epoch,
            participation_plan_id=plan.id, participation_unit=plan.participation_unit,
            planning_horizon="historical-test", dependency_revision_set_hash="historical",
            account_paths=PATHS, admissible_account_ids=[12], deficit_account_ids=[11],
            decision="partially_serviceable", decision_hash="historical")
        session.add(row)
        session.flush()
        identity = row.id
        session.commit()
        return identity


def test_historical_conversion_real_foreign_keys_and_tenant_isolation():
    identity = _seed_historical_snapshot()
    try:
        with SessionLocal() as session:
            manifest = preview_admission_evidence(session, batch_size=100)
            assert manifest["count"] == 1
            apply_admission_evidence(session, manifest=manifest, context=CONTEXT)
            session.commit()
        with SessionLocal() as session:
            result = readback_admission_evidence(session, fingerprint=manifest["fingerprint"], context=CONTEXT)
            assert result["persisted_verified"]
            assert session.get(PlanningAdmissionSnapshot, identity).account_paths == PATHS
            assert session.scalar(text("SELECT account_paths FROM planning_admission_snapshots WHERE id=:id"), {"id": identity}) == []
            session.add(Tenant(id=2, name="other-tenant"))
            session.flush()
            other = shared_admission_evidence(session, 2, [{"other_tenant_only": True}])
            session.get(PlanningAdmissionSnapshot, identity).paths_evidence = other
            session.get(PlanningAdmissionSnapshot, identity).account_paths_digest = other.digest
            with pytest.raises(IntegrityError):
                session.flush()
            session.rollback()
    finally:
        with SessionLocal.begin() as session:
            session.execute(delete(Task).where(Task.tenant_id == 1))
            session.execute(delete(Tenant).where(Tenant.id == 1))

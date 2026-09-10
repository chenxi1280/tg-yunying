from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import event, func, select

from app.models import PlanningAdmissionEvidence, PlanningAdmissionSnapshot, Tenant
from app.services.task_center.admission_evidence_maintenance import (
    apply_admission_evidence, preview_admission_evidence, readback_admission_evidence,
)
from app.services.task_center.planning_admission_evidence import (
    admission_paths_digest, shared_admission_evidence,
)
from app.services.task_center.runtime_storage_maintenance import MaintenanceContext
from tests.test_engagement_participation import _session


pytestmark = pytest.mark.no_postgres
CONTEXT = MaintenanceContext("production", "a" * 40, "a" * 40, "resource-repair", "test-evidence")
PATHS = [{"account_id": 12, "admissible": True, "revision": {"epoch": 3}},
         {"account_id": 11, "admissible": False, "reason": "观察待完成"}]


def _snapshot(identity, paths=PATHS):
    return PlanningAdmissionSnapshot(id=identity, tenant_id=1, task_id="task",
        task_lifecycle_epoch=1, participation_plan_id="plan", participation_unit="unit",
        planning_horizon=identity, dependency_revision_set_hash=identity, account_paths=paths,
        admissible_account_ids=[12], deficit_account_ids=[11], decision="partially_serviceable",
        decision_hash=identity, created_at=datetime(2026, 9, 10, tzinfo=timezone.utc))


def test_shared_paths_preserve_order_values_and_isolate_tenants_and_changes():
    with _session() as session:
        session.add_all([Tenant(id=1, name="one"), Tenant(id=2, name="two")])
        first = shared_admission_evidence(session, 1, PATHS)
        second = shared_admission_evidence(session, 1, PATHS)
        other_tenant = shared_admission_evidence(session, 2, PATHS)
        changed = shared_admission_evidence(session, 1, list(reversed(PATHS)))
        session.commit()
        assert first is second
        assert first.digest == other_tenant.digest and first.tenant_id != other_tenant.tenant_id
        assert changed.digest != first.digest
        assert first.account_paths == PATHS
        assert session.scalar(select(func.count()).select_from(PlanningAdmissionEvidence)) == 3


def test_shared_paths_do_not_alias_input_or_survive_transaction_rollback():
    with _session() as session:
        session.add(Tenant(id=1, name="one"))
        session.commit()
        source = [{"account_id": 1}]
        record = shared_admission_evidence(session, 1, source)
        source[0]["account_id"] = 2
        assert record.account_paths == [{"account_id": 1}]
        session.rollback()
        assert session.scalar(select(func.count()).select_from(PlanningAdmissionEvidence)) == 0


def test_historical_conversion_preserves_full_identity_content_and_readback():
    with _session() as session:
        session.add_all([Tenant(id=1, name="one"), _snapshot("first"), _snapshot("second")])
        session.commit()
        manifest = preview_admission_evidence(session, batch_size=2)
        assert manifest["count"] == 2
        apply_admission_evidence(session, manifest=manifest, context=CONTEXT)
        session.commit()
        session.expunge_all()
        snapshots = list(session.scalars(select(PlanningAdmissionSnapshot)))
        assert [row.id for row in snapshots] == ["first", "second"]
        assert all(row.account_paths == PATHS and row.legacy_account_paths == [] for row in snapshots)
        assert session.scalar(select(func.count()).select_from(PlanningAdmissionEvidence)) == 1
        result = readback_admission_evidence(session, fingerprint=manifest["fingerprint"], context=CONTEXT)
        assert result["persisted_verified"] and result["count"] == 2
        with pytest.raises(RuntimeError, match="snapshot_drift"):
            apply_admission_evidence(session, manifest=manifest, context=CONTEXT)


def test_historical_conversion_rejects_changed_payload_without_writes():
    with _session() as session:
        session.add_all([Tenant(id=1, name="one"), _snapshot("first")])
        session.commit()
        manifest = preview_admission_evidence(session, batch_size=1)
        session.get(PlanningAdmissionSnapshot, "first").account_paths = [{"changed": True}]
        session.commit()
        with pytest.raises(RuntimeError, match="snapshot_drift"):
            apply_admission_evidence(session, manifest=manifest, context=CONTEXT)
        assert session.scalar(select(func.count()).select_from(PlanningAdmissionEvidence)) == 0


def test_historical_conversion_rollback_preserves_legacy_and_has_no_audit():
    with _session() as session:
        session.add_all([Tenant(id=1, name="one"), _snapshot("first")])
        session.commit()
        manifest = preview_admission_evidence(session, batch_size=1)
        apply_admission_evidence(session, manifest=manifest, context=CONTEXT)
        session.rollback()
        assert session.get(PlanningAdmissionSnapshot, "first").account_paths == PATHS
        assert session.get(PlanningAdmissionSnapshot, "first").account_paths_digest is None
        assert session.scalar(select(func.count()).select_from(PlanningAdmissionEvidence)) == 0
        with pytest.raises(RuntimeError, match="audit_missing"):
            readback_admission_evidence(session, fingerprint=manifest["fingerprint"], context=CONTEXT)


def test_missing_referenced_evidence_is_an_error_and_shared_paths_are_immutable():
    record = _snapshot("first")
    record.account_paths_digest = admission_paths_digest(PATHS)
    with pytest.raises(RuntimeError, match="evidence_missing"):
        _ = record.account_paths
    with pytest.raises(ValueError, match="immutable"):
        record.account_paths = []


def test_reaction_admissible_ids_query_does_not_load_snapshot_payload(monkeypatch):
    from app.services.task_center import engagement_reaction_capacity as capacity
    with _session() as session:
        session.add(_snapshot("first"))
        session.commit()
        session.expunge_all()
        monkeypatch.setattr(capacity, "_reaction_candidates", lambda *a, **k: ([], [], ["first"]))
        statements = []
        connection = session.connection()
        def record(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)
        event.listen(connection, "before_cursor_execute", record)
        result = capacity.reaction_admissible_account_ids(session, SimpleNamespace(source_demands=[]),
            task=SimpleNamespace(), ledger=SimpleNamespace(), target=SimpleNamespace())
        event.remove(connection, "before_cursor_execute", record)
        assert result == {12}
        assert len(statements) == 1
        assert "account_paths" not in statements[0]
        assert not list(session.identity_map.values())

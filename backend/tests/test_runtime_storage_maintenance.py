from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Action, RuntimeCleanupAudit, Task, Tenant
from app.services.task_center.runtime_storage_maintenance import (
    MaintenanceContext,
    apply_runtime_details_batch,
    preview_runtime_details,
    readback_runtime_details,
)


pytestmark = pytest.mark.no_postgres
AS_OF = datetime(2026, 8, 30, 22, 30, tzinfo=timezone.utc)
RELEASE_SHA = "a" * 40


def test_guarded_batch_preview_apply_and_independent_readback() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    context = _context()
    with Session(engine) as session:
        _seed(session)
        preview = preview_runtime_details(session, as_of=AS_OF, batch_size=10)

        assert preview["candidate_count"] == 1
        assert preview["apply_allowed"] is True
        result = apply_runtime_details_batch(
            session,
            context=context,
            as_of=AS_OF,
            expected_fingerprint=preview["candidate_fingerprint"],
            expected_count=1,
            batch_size=10,
        )
        session.commit()

        readback = readback_runtime_details(
            session,
            context=context,
            expected_fingerprint=preview["candidate_fingerprint"],
        )
        audit = session.query(RuntimeCleanupAudit).filter(
            RuntimeCleanupAudit.summary["candidate_fingerprint"].as_string()
            == preview["candidate_fingerprint"]
        ).one()

        assert result["affected_rows"] == 1
        assert readback["persisted_verified"] is True
        assert audit.summary["actor"] == "codex"
        assert audit.summary["approval_ref"] == "database-storage-optimization-20260830"


def test_apply_rejects_preview_drift_before_delete() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        _seed(session)

        with pytest.raises(RuntimeError, match="candidate_fingerprint_drift"):
            apply_runtime_details_batch(
                session,
                context=_context(),
                as_of=AS_OF,
                expected_fingerprint="b" * 64,
                expected_count=1,
                batch_size=10,
            )

        assert session.get(Action, "expired-action") is not None


def test_apply_rejects_empty_batch_instead_of_reporting_fake_success() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Tenant(id=1, name="tenant"))
        session.commit()
        empty = preview_runtime_details(session, as_of=AS_OF, batch_size=10)

        with pytest.raises(RuntimeError, match="runtime_storage_no_candidates"):
            apply_runtime_details_batch(
                session,
                context=_context(),
                as_of=AS_OF,
                expected_fingerprint=empty["candidate_fingerprint"],
                expected_count=0,
                batch_size=10,
            )


@pytest.mark.parametrize(
    "context",
    [
        MaintenanceContext("staging", RELEASE_SHA, RELEASE_SHA, "codex", "approval"),
        MaintenanceContext("production", "b" * 40, RELEASE_SHA, "codex", "approval"),
        MaintenanceContext("production", RELEASE_SHA, RELEASE_SHA, "", "approval"),
    ],
)
def test_maintenance_context_rejects_scope_mismatch(context: MaintenanceContext) -> None:
    with pytest.raises(ValueError):
        context.validate()


def _context() -> MaintenanceContext:
    return MaintenanceContext(
        environment="production",
        expected_release_sha=RELEASE_SHA,
        current_release_sha=RELEASE_SHA,
        actor="codex",
        approval_ref="database-storage-optimization-20260830",
    )


def _seed(session: Session) -> None:
    old_at = datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc)
    session.add(Tenant(id=1, name="tenant"))
    session.add(Task(id="task", tenant_id=1, name="task", type="group_relay", status="running"))
    session.add(Action(
        id="expired-action",
        tenant_id=1,
        task_id="task",
        task_type="group_relay",
        action_type="send_message",
        status="success",
        result={"generation_outcome": "ready"},
        scheduled_at=old_at,
        executed_at=old_at,
        created_at=old_at,
    ))
    session.commit()

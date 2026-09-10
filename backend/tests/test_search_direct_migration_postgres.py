import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from tests.owner_postgres_support import owner_engine
from app.models import Action, AuditLog, Task, Tenant
from app.services._common import _now
from app.services.task_center.search_direct_migration import (
    SearchDirectMigration, apply_search_direct_migration, preview_search_direct_migration,
)

pytestmark = pytest.mark.isolated_postgres
SCOPE = SearchDirectMigration(1, ("task",), "tester", "direct-cutover-test")


def seed(session):
    session.add(Tenant(id=1, name="test"))
    session.flush()
    task = Task(id="task", tenant_id=1, name="test", type="search_click", status="running",
                type_config={"daily_click_target_count": 1000}, stats={"confirmed_count": 378})
    session.add(task)
    session.flush()
    action = Action(id="unknown", tenant_id=1, task_id=task.id, task_type=task.type,
                    action_type="search_join", status="unknown_after_send", scheduled_at=_now(),
                    action_dedupe_key="unknown", payload={"callback_fingerprint": "original"},
                    result={"target_click_observed": True})
    session.add(action)
    session.commit()
    return task, action


def test_migration_preserves_goal_progress_and_unknown_identity(owner_engine):
    with Session(owner_engine) as session:
        task, action = seed(session)
        before = preview_search_direct_migration(session, SCOPE)
        after = apply_search_direct_migration(session, SCOPE, before)
        session.commit()
        session.expire_all()
        assert after[task.id]["protected_hash"] == before[task.id]["protected_hash"]
        assert task.type_config == {"daily_click_target_count": 1000,
                                    "transport_contract_version": "sv_current_direct_v1"}
        assert task.stats == {"confirmed_count": 378}
        assert action.status == "unknown_after_send" and action.payload == {"callback_fingerprint": "original"}
        assert action.result == {"target_click_observed": True}
        audit = session.scalar(select(AuditLog).where(AuditLog.target_id == task.id))
        assert "direct-cutover-test" in audit.detail


def test_stale_preview_rejects_without_configuration_change(owner_engine):
    with Session(owner_engine) as session:
        task, _ = seed(session)
        before = preview_search_direct_migration(session, SCOPE)
        task.type_config = {"daily_click_target_count": 1200}
        session.commit()
        with pytest.raises(ValueError, match="stale_preview"):
            apply_search_direct_migration(session, SCOPE, before)
        session.rollback()
        assert task.type_config == {"daily_click_target_count": 1200}
        assert session.scalar(select(AuditLog)) is None


def test_other_tenant_and_nonsearch_task_are_rejected(owner_engine):
    with Session(owner_engine) as session:
        task, _ = seed(session)
        with pytest.raises(ValueError, match="scope_mismatch"):
            preview_search_direct_migration(session, SearchDirectMigration(2, ("task",), "tester", "test"))
        task.type = "group_ai_chat"
        session.commit()
        with pytest.raises(ValueError, match="scope_mismatch"):
            preview_search_direct_migration(session, SCOPE)

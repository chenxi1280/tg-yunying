"""Real PostgreSQL locking tests; only an explicitly named test database is allowed."""
from concurrent.futures import ThreadPoolExecutor
import os
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Action, ExecutionAttempt, OperationTarget, SourcePacingAdmission, SourcePacingState
from app.services.task_center.channel_membership_runtime import membership_runtime_wait
from app.services.task_center.stale_source_admissions import apply_stale_admissions, preview_stale_admissions
from tests.test_channel_membership_window_runtime import (
    NOW, add_running_attempt, membership_action, seed_membership_runtime,
)
from tests.test_stale_source_admissions import SCOPE


pytestmark = pytest.mark.isolated_postgres


@pytest.fixture
def engine(postgres_test_session_lock):
    url = make_url(os.environ["TEST_DATABASE_URL"])
    assert url.database == "tg_yunying_test"
    admin = create_engine(url)
    schema = "membership_review_" + uuid4().hex
    with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as lock:
        lock.execute(text(f'CREATE SCHEMA "{schema}"'))
        scoped = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
        try:
            Base.metadata.create_all(scoped)
            with Session(scoped) as session:
                seed_membership_runtime(session)
            yield scoped
        finally:
            scoped.dispose()
            lock.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    admin.dispose()


def test_three_workers_admit_only_two_gateway_attempts(engine):
    with Session(engine) as session:
        session.add_all([membership_action(number) for number in (1, 2, 3)])
        session.commit()
    barrier = Barrier(3)

    def worker(number):
        with Session(engine) as session:
            action = session.get(Action, f"action-{number}")
            target = session.get(OperationTarget, 1)
            barrier.wait(timeout=10)
            wait = membership_runtime_wait(session, action, target=target, now=NOW)
            if wait is None:
                add_running_attempt(session, action)
            session.commit()
            return wait is None

    with ThreadPoolExecutor(max_workers=3) as pool:
        outcomes = list(pool.map(worker, (1, 2, 3)))
    assert sum(outcomes) == 2
    with Session(engine) as session:
        assert session.query(ExecutionAttempt).count() == 2


def recovery_preview(engine):
    with Session(engine) as session:
        action = membership_action(1)
        action.status = "failed"
        session.add(action)
        session.add(SourcePacingState(id="state", tenant_id=1, pacing_domain="reaction",
                                      source_key_hash="f" * 64, next_call_not_before_at=NOW))
        session.flush()
        session.add(SourcePacingAdmission(
            id="admission", admission_key="recovery", tenant_id=1, task_id=action.task_id,
            source_pacing_state_id="state", owner_type="test", owner_id="owner", action_id=action.id,
            pacing_period_key="day", pacing_plan_hash="f" * 64, planned_release_at=NOW,
            call_not_before_at=NOW, source_gap_seconds=864,
        ))
        session.commit()
        session.execute(text("SET TRANSACTION READ ONLY"))
        return preview_stale_admissions(session, SCOPE)


def test_apply_rejects_locked_action_without_cancelling_reservation(engine):
    preview = recovery_preview(engine)
    with Session(engine) as blocker, Session(engine) as applying:
        blocker.scalar(select(Action).where(Action.id == "action-1").with_for_update())
        with pytest.raises(OperationalError):
            apply_stale_admissions(applying, preview, actor="test", audit_reference="locked")
        applying.rollback()
        assert applying.get(SourcePacingAdmission, "admission").state == "reserved"


def test_apply_matches_read_only_preview_and_commits_audit(engine):
    preview = recovery_preview(engine)
    assert preview["candidate_ids"] == ["admission"]
    assert preview["states_after"] == {"state": None}
    with Session(engine) as session, session.begin():
        receipt = apply_stale_admissions(session, preview, actor="test", audit_reference="pg-proof")
    assert receipt["states_after"] == {"state": None}

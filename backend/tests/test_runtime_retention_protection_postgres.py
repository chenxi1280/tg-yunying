"""Exercise retention against actual FK cascades and concurrent PostgreSQL writers."""
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateSchema, DropSchema

from app.database import Base
from app.models import (
    Action, ChannelMessage, CommentFulfillmentObligation, ExecutionAttempt,
    OperationTarget, ReactionFulfillmentObligation, TaskDayLedger, Tenant,
    ViewFulfillmentObligation,
)
from app.services.task_center import recent_success as recent
from app.services.task_center import runtime_retention as retention
from app.services.task_center.runtime_storage_maintenance import preview_runtime_details
from app.timezone import as_beijing_aware
from tests.test_recent_task_success import NOW, _record, _task


pytestmark = [pytest.mark.isolated_postgres, pytest.mark.allow_missing_rule_binding]


@pytest.fixture
def database(postgres_test_session_lock):
    url = os.environ["TEST_DATABASE_URL"]
    admin = create_engine(url)
    schema = "retention_p1_" + uuid4().hex
    with admin.begin() as connection:
        assert connection.scalar(text("select current_database()")) == "tg_yunying_test"
        connection.execute(CreateSchema(schema))
    engine = create_engine(url, connect_args={"options": f"-c search_path={schema} -c lock_timeout=3s -c statement_timeout=20s"})
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()
    with admin.begin() as connection:
        connection.execute(DropSchema(schema, cascade=True))
    admin.dispose()


@pytest.fixture
def guarded_database(postgres_test_session_lock, database):
    return database


def _seed(session):
    session.add(Tenant(id=1, name="retention-p1"))
    session.flush()
    task = _task(session, task_type="channel_like")
    action, attempt, fact = _record(session, task, when=as_beijing_aware(NOW - timedelta(hours=66)))
    action.executed_at = NOW - timedelta(days=10)
    session.add(OperationTarget(id=101, tenant_id=1, target_type="channel", tg_peer_id="-100101", title="QA"))
    session.flush()
    session.add(ChannelMessage(id=101, tenant_id=1, channel_target_id=101, message_id=900))
    session.flush()
    return task, action, attempt, fact


def _obligation(task_id, action_id):
    return ReactionFulfillmentObligation(id=str(uuid4()), tenant_id=1,
        task_id=task_id, channel_message_id=101, account_id=11,
        reaction_contract_version=1, current_action_id=action_id, status="confirmed")


def test_actual_fk_cleanup_keeps_business_owner_and_rolling_statistics(guarded_database):
    with Session(guarded_database) as session:
        task, action, attempt, fact = _seed(session)
        before = recent.recent_task_success(session, task, now_value=NOW)
        assert before["success_count"] == 1
        assert retention.cleanup_runtime_details(session, as_of=NOW) == 0
        assert recent.recent_task_success(session, task, now_value=NOW) == before
        fact.observed_at = as_beijing_aware(NOW - timedelta(days=4))
        session.add(_obligation(task.id, action.id))
        session.flush()
        preview = preview_runtime_details(session, as_of=NOW)
        assert preview["candidate_count"] == 0
        assert preview["protected_dependencies"]["reference_counts"]["reaction_fulfillment_obligations.current_action_id"] == 1
        assert retention.cleanup_runtime_details(session, as_of=NOW) == 0
        assert session.scalar(select(ReactionFulfillmentObligation.id)) is not None
        assert session.get(ExecutionAttempt, attempt.id) is attempt
        assert session.get(Action, action.id) is action
        session.rollback()


def test_dependency_added_after_preview_is_rechecked_before_delete(guarded_database, monkeypatch):
    with Session(guarded_database) as session:
        task, action, _, fact = _seed(session)
        fact.observed_at = as_beijing_aware(NOW - timedelta(days=4))
        session.flush()
        original = retention._apply_retention_batch

        def apply_with_new_dependency(current, **kwargs):
            current.add(_obligation(task.id, action.id))
            current.flush()
            return original(current, **kwargs)

        monkeypatch.setattr(retention, "_apply_retention_batch", apply_with_new_dependency)
        with pytest.raises(RuntimeError, match="runtime_retention_dependency_changed"):
            retention.cleanup_runtime_details(session, as_of=NOW)
        assert session.get(Action, action.id) is action
        session.rollback()


def test_fk_writer_cannot_attach_business_owner_after_cleanup_claim(guarded_database):
    with Session(guarded_database) as seed:
        task, action, _, fact = _seed(seed)
        fact.observed_at = as_beijing_aware(NOW - timedelta(days=4))
        task_id, action_id = task.id, action.id
        seed.commit()
    with Session(guarded_database) as cleaner:
        rows = retention._runtime_detail_batch(cleaner,
            retention.DEFAULT_RUNTIME_ACTION_RETENTION_POLICY.cutoffs(NOW), 10, as_of=NOW)
        assert [row.id for row in rows] == [action_id]
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(_try_attach, guarded_database, task_id, action_id)
            assert result.result(timeout=5) == "55P03"
        cleaner.rollback()


def _try_attach(engine, task_id, action_id):
    from sqlalchemy.exc import DBAPIError
    with Session(engine) as writer:
        writer.add(_obligation(task_id, action_id))
        try:
            writer.commit()
        except DBAPIError as error:
            writer.rollback()
            return error.orig.sqlstate
    return "committed"


@pytest.mark.parametrize("kind", ("comment", "view"))
def test_other_cascade_owners_survive_expired_action_cleanup(guarded_database, kind):
    with Session(guarded_database) as session:
        task, action, _, fact = _seed(session)
        fact.observed_at = as_beijing_aware(NOW - timedelta(days=4))
        common = dict(tenant_id=1, channel_message_id=101, account_id=11,
            current_action_id=action.id, status="confirmed")
        if kind == "comment":
            owner = CommentFulfillmentObligation(**common, task_id=task.id,
                comment_plan_revision=1, target_ordinal=1)
        else:
            ledger = TaskDayLedger(tenant_id=1, task_id=task.id,
                timezone_snapshot="Asia/Shanghai", timezone_revision=1,
                obligation_local_date=NOW.date(), period_start_at=NOW,
                deadline_at=NOW + timedelta(days=1), day_phase="full",
                planning_anchor_at=NOW)
            session.add(ledger)
            session.flush()
            owner = ViewFulfillmentObligation(**common, task_day_ledger_id=ledger.id)
        session.add(owner)
        session.flush()
        owner_id, action_id, fact_id = owner.id, action.id, fact.fact_id
        assert retention.cleanup_runtime_details(session, as_of=NOW) == 0
        session.expire_all()
        assert session.get(type(owner), owner_id).current_action_id == action_id
        assert session.get(type(fact), fact_id) is not None

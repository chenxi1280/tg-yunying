from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Action, ExecutionAttempt, FulfillmentRemoteFact, Task, Tenant, TgAccount
from app.services.task_center import recent_success as recent


pytestmark = pytest.mark.no_postgres
NOW = datetime(2026, 9, 4, 12)


@pytest.fixture(scope="module")
def database():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def session(database):
    with Session(database, autoflush=False) as current:
        current.add_all([Tenant(id=1, name="QA"), Tenant(id=2, name="QA isolated")])
        current.flush()
        yield current


def _task(session, *, task_type="group_ai_chat", tenant_id=1):
    task = Task(id=str(uuid4()), tenant_id=tenant_id, name="QA", type=task_type, status="running")
    session.add(task)
    session.flush()
    return task


def _record(session, task, *, when=NOW, account_id=11, action=None):
    action_type, kind, _label = recent.SUCCESS_KINDS[task.type]
    if account_id is not None and session.get(TgAccount, account_id) is None:
        session.add(TgAccount(id=account_id, tenant_id=task.tenant_id, display_name="QA account", phone_masked="QA"))
        session.flush()
    if action is None:
        action = Action(id=str(uuid4()), tenant_id=task.tenant_id, task_id=task.id,
                        task_type=task.type, action_type=action_type, account_id=account_id, status="success")
        session.add(action)
        session.flush()
    attempt = ExecutionAttempt(id=str(uuid4()), tenant_id=task.tenant_id, action_id=action.id,
        account_id=account_id, attempt_no=1,
        status="success", after_call_at=when, remote_message_id="900")
    # Multiple observations can reference the same original execution attempt.
    existing = session.query(ExecutionAttempt).filter_by(action_id=action.id).first()
    if existing is not None:
        attempt = existing
    else:
        session.add(attempt)
        session.flush()
    fact = FulfillmentRemoteFact(fact_id=str(uuid4()), tenant_id=task.tenant_id, task_id=task.id,
        task_type=task.type, obligation_type=action_type, obligation_id=action.id,
        action_id=action.id, attempt_id=attempt.id, mutation_kind=action_type, fact_kind=kind,
        remote_mutation_key_hash=action.id.ljust(64, "0"), gateway_request_hash=uuid4().hex.ljust(64, "0"),
        fact_identity_hash=uuid4().hex.ljust(64, "0"), observed_at=when,
        outcome={"action_status": "success", "attempt_status": "success", "remote_message_id": "900"})
    session.add(fact)
    session.flush()
    return action, attempt, fact


@pytest.mark.parametrize("task_type", tuple(recent.SUCCESS_KINDS))
def test_each_task_type_counts_only_its_typed_success(session, task_type):
    task = _task(session, task_type=task_type)
    _record(session, task)
    result = recent.recent_task_success(session, task, now_value=NOW)
    assert result["success_count"] == 1
    assert result["metric_label"] == recent.SUCCESS_KINDS[task_type][2]
    assert result["account_counts"] == [{"account_id": 11, "success_count": 1}]


@pytest.mark.parametrize(("offset", "expected"), (
    (timedelta(hours=-72), 1), (timedelta(hours=-72, microseconds=-1), 0),
    (timedelta(0), 1), (timedelta(microseconds=1), 0),
))
def test_confirmation_window_boundaries(session, offset, expected):
    task = _task(session)
    _record(session, task, when=NOW + offset)
    assert recent.recent_task_success(session, task, now_value=NOW)["success_count"] == expected


def test_repeated_old_confirmation_does_not_reenter_window(session):
    task = _task(session)
    action, _, _ = _record(session, task, when=NOW - timedelta(days=4))
    _record(session, task, action=action, when=NOW - timedelta(hours=1))
    assert recent.recent_task_success(session, task, now_value=NOW)["success_count"] == 0


def test_duplicate_confirmation_counts_once(session):
    task = _task(session)
    action, _, _ = _record(session, task, when=NOW - timedelta(hours=1))
    _record(session, task, action=action)
    assert recent.recent_task_success(session, task, now_value=NOW)["success_count"] == 1


@pytest.mark.parametrize("invalid", ("unknown", "failed", "empty_remote", "missing_remote", "wrong_kind", "wrong_mutation"))
def test_invalid_facts_never_count_as_sent(session, invalid):
    task = _task(session)
    _, _, fact = _record(session, task)
    if invalid == "unknown":
        fact.fact_kind = "remote_outcome_unknown"
    elif invalid == "failed":
        fact.outcome = {**fact.outcome, "attempt_status": "failed"}
    elif invalid in {"empty_remote", "missing_remote"}:
        fact.outcome = {**fact.outcome, "remote_message_id": "  " if invalid == "empty_remote" else None}
    elif invalid == "wrong_kind":
        fact.fact_kind = "reaction_observed"
    else:
        fact.mutation_kind = "ensure_target_membership"
    session.flush()
    assert recent.recent_task_success(session, task, now_value=NOW)["success_count"] == 0


def test_task_and_tenant_isolation(session):
    task = _task(session)
    other_task = _task(session)
    other_tenant = _task(session, tenant_id=2)
    _record(session, task)
    _record(session, other_task)
    _, _, wrong = _record(session, other_tenant, account_id=21)
    wrong.task_id = task.id
    session.flush()
    assert recent.recent_task_success(session, task, now_value=NOW)["success_count"] == 1


def test_original_attempt_account_survives_current_state_changes(session):
    task = _task(session)
    action, _, _ = _record(session, task)
    action.status, action.account_id, task.status = "cancelled", None, "paused"
    task.account_config = {"account_group_ids": [999]}
    session.flush()
    result = recent.recent_task_success(session, task, now_value=NOW)
    assert result["account_counts"] == [{"account_id": 11, "success_count": 1}]


def test_account_totals_include_unassigned_success(session):
    task = _task(session)
    for account_id in (11, 11, 12, None):
        _record(session, task, account_id=account_id)
    result = recent.recent_task_success(session, task, now_value=NOW)
    assert result["success_count"] == 4 and result["unassigned_count"] == 1
    assert result["account_counts"] == [{"account_id": 11, "success_count": 2}, {"account_id": 12, "success_count": 1}]


def test_empty_and_offset_window(session):
    task = _task(session)
    result = recent.recent_task_success(session, task, now_value=NOW.replace(tzinfo=timezone(timedelta(hours=8))))
    assert result["success_count"] == 0 and result["account_counts"] == []
    assert result["window_hours"] == 72
    assert result["window_start"] == "2026-09-01T12:00:00+08:00"


def test_detail_live_stats_wires_counts_without_persisting_window(session, monkeypatch):
    from app.services.task_center import details

    task = _task(session, task_type="channel_view")
    _record(session, task)
    monkeypatch.setattr(recent, "_now", lambda: NOW)
    monkeypatch.setattr(details, "task_account_coverage", lambda *_: {})
    original = {"success_count": 999}
    result = details._stats_with_account_coverage(session, task, original)
    assert result["recent_success"]["success_count"] == 1
    assert original == {"success_count": 999} and "recent_success" not in task.stats


def test_unrelated_tasks_do_not_query_or_show_metric():
    from types import SimpleNamespace

    assert recent.recent_task_success(None, SimpleNamespace(type="group_relay")) is None

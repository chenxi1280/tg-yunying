from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Task
from tests.test_task_fulfillment_e4_diagnostics import load_module


pytestmark = pytest.mark.no_postgres
SINCE = datetime(2026, 9, 9, 12)
READ_ONLY_COMMANDS = [
    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY",
    "SET LOCAL statement_timeout = '20s'",
    "SET LOCAL lock_timeout = '2s'",
]


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as value:
        yield value
    engine.dispose()


def _task(session, task_id, **changes):
    values = dict(id=task_id, tenant_id=1, name="scope regression", type="channel_view",
        status="running", updated_at=SINCE)
    task = Task(**{**values, **changes})
    session.add(task)
    session.flush()
    return task


@pytest.mark.parametrize("raw", ["", "__discover_channel_view__"])
def test_discovery_includes_all_eleven_eligible_tasks(session, monkeypatch, raw):
    module = load_module()
    monkeypatch.setenv(module.TASK_IDS_ENV, raw)
    ids = [f"view-{index:02}" for index in range(10)]
    for task_id in reversed(ids):
        _task(session, task_id)
    _task(session, "newest", status="completed", updated_at=SINCE + timedelta(seconds=1))
    assert module.parse_task_ids(session) == ["newest", *ids]


def test_discovery_excludes_deleted_and_ineligible_tasks(session, monkeypatch):
    module = load_module()
    monkeypatch.delenv(module.TASK_IDS_ENV, raising=False)
    _task(session, "valid")
    _task(session, "deleted", deleted_at=SINCE)
    _task(session, "stopped", status="stopped")
    _task(session, "group", type="group_ai_chat")
    assert module.parse_task_ids(session) == ["valid"]


def test_explicit_scope_keeps_order_and_deduplicates_without_discovery(monkeypatch):
    module = load_module()
    monkeypatch.setenv(module.TASK_IDS_ENV, " deleted, missing, deleted, ")
    session = MagicMock()
    assert module.parse_task_ids(session) == ["deleted", "missing"]
    session.scalars.assert_not_called()


@pytest.mark.parametrize("deleted", [False, True])
def test_explicit_task_snapshot_exposes_deletion(session, deleted):
    task = _task(session, "explicit", status="completed", deleted_at=SINCE if deleted else None)
    module = load_module()
    snapshot = module.task_snapshot(session, task.id, SINCE)
    assert snapshot["task_deleted"] is deleted
    assert snapshot["task_status"] == "completed"
    assert ("task_deleted" in module.e4_blockers(snapshot)) is deleted


@pytest.mark.parametrize("task_type", ["group_ai_chat", "search_click", "channel_view"])
def test_deleted_task_cannot_pass_with_otherwise_sufficient_evidence(task_type):
    snapshot = dict(task_type=task_type, task_status="completed", ledger_id="ledger",
        group_daily=dict(target_row_count=1, due_message_count=1, confirmed_message_count=1,
            coverage_required_count=1, coverage_confirmed_count=1, post_release_remote_fact_count=1),
        search_click=dict(required_count=1, confirmed_count=1, post_release_confirmed_count=1),
        channel_view=dict(required_count=1, materialized_count=1, confirmed_count=1,
            remote_fact_count=1, post_release_remote_fact_count=1))
    module = load_module()
    assert module.e4_blockers(snapshot) == []
    assert module.e4_blockers({**snapshot, "task_deleted": True}) == ["task_deleted"]


def _cli(monkeypatch):
    module = load_module()
    session = MagicMock()
    session.__enter__.return_value = session
    monkeypatch.setattr(module, "SessionLocal", lambda: session)
    monkeypatch.setattr(module, "parse_release_since", lambda: SINCE)
    return module, session


def test_cli_configures_readonly_snapshot_before_first_read(monkeypatch, capsys):
    module, session = _cli(monkeypatch)
    events = []
    session.execute.side_effect = lambda statement: events.append(str(statement))
    monkeypatch.setattr(module, "parse_task_ids", lambda value: events.append("discover") or ["task"])
    monkeypatch.setattr(module, "task_snapshot", lambda *args: events.append("snapshot") or
        {"task_id": "task", "task_status": "missing"})
    with pytest.raises(SystemExit, match="E4 gate failed"):
        module.main()
    assert events == [*READ_ONLY_COMMANDS, "discover", "snapshot"]
    assert '"task_missing"' in capsys.readouterr().out
    session.commit.assert_not_called()
    session.__exit__.assert_called_once()


@pytest.mark.parametrize("failed_command", range(len(READ_ONLY_COMMANDS)))
def test_failed_transaction_setup_stops_before_reading_or_reporting(monkeypatch, capsys, failed_command):
    module, session = _cli(monkeypatch)
    session.execute.side_effect = [None] * failed_command + [RuntimeError("readonly transaction rejected")]
    discover = MagicMock(return_value=["task"])
    monkeypatch.setattr(module, "parse_task_ids", discover)
    monkeypatch.setattr(module, "task_snapshot", MagicMock(return_value={"task_id": "task"}))
    with pytest.raises(RuntimeError, match="readonly transaction rejected"):
        module.main()
    discover.assert_not_called()
    module.task_snapshot.assert_not_called()
    assert capsys.readouterr().out == ""
    session.commit.assert_not_called()
    session.__exit__.assert_called_once()


def test_query_failure_does_not_emit_a_summary(monkeypatch, capsys):
    module, session = _cli(monkeypatch)
    monkeypatch.setattr(module, "parse_task_ids", lambda value: ["task"])
    monkeypatch.setattr(module, "task_snapshot", MagicMock(side_effect=RuntimeError("query timeout")))
    with pytest.raises(RuntimeError, match="query timeout"):
        module.main()
    assert capsys.readouterr().out == ""
    session.commit.assert_not_called()
    session.__exit__.assert_called_once()

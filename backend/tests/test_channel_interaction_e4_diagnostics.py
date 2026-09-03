from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import ReactionFulfillmentObligation


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = PROJECT_ROOT / ".github/scripts/channel_interaction_e4_diagnostics.py"
pytestmark = pytest.mark.no_postgres


def load_module():
    spec = importlib.util.spec_from_file_location("channel_interaction_e4_diagnostics", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _obligation(obligation_id: str, status: str, due_at: datetime | None):
    return ReactionFulfillmentObligation(
        id=obligation_id,
        tenant_id=1,
        task_id="task-like",
        channel_message_id=1,
        account_id={"confirmed": 1, "open": 2, "expired": 3, "missing": 4}[obligation_id],
        reaction_contract_version=1,
        status=status,
        pacing_due_at=due_at,
    )


def test_due_counts_exclude_closed_expired_without_counting_it_as_confirmed() -> None:
    module = load_module()
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now = datetime(2026, 8, 30, 8, tzinfo=timezone.utc)
    with Session(engine) as session:
        session.add_all([
            _obligation("confirmed", "confirmed", now - timedelta(minutes=4)),
            _obligation("open", "open", now - timedelta(minutes=3)),
            _obligation("expired", "closed_expired", now - timedelta(minutes=2)),
            _obligation("missing", "open", None),
        ])
        session.flush()

        counts = module._due_counts(
            session,
            ReactionFulfillmentObligation,
            ReactionFulfillmentObligation.task_id == "task-like",
            now,
        )

    assert counts == {"due": 2, "due_confirmed": 1, "due_at_missing": 1}


def test_task_ids_env_parses_unique_non_empty_values(monkeypatch) -> None:
    module = load_module()
    monkeypatch.setenv(module.TASK_IDS_ENV, " task-a,task-b, task-a ,,task-c ")

    assert module._task_ids_from_env() == ["task-a", "task-b", "task-c"]


def test_closed_expired_only_snapshot_is_missed_not_met() -> None:
    module = load_module()
    task = type("TaskSnapshot", (), {"status": "running"})()
    blockers = module._blockers(
        task,
        obligations={
            "status_counts": {"closed_expired": 3},
            "due": 0,
            "due_confirmed": 0,
            "post_release_remote_fact_count": 0,
        },
        actions={"due_lifecycle_mismatch_count": 0, "due_open_count": 0},
        attempts={"post_release_count": 0},
    )

    assert blockers == ["interaction_expired_unmet"]
    assert module._goal_status(task, blockers) == "missed"


def test_paused_task_remains_visible_without_becoming_met() -> None:
    module = load_module()
    task = type("TaskSnapshot", (), {"status": "paused"})()
    blockers = module._blockers(
        task,
        obligations={
            "status_counts": {"open": 3},
            "due": 0,
            "due_confirmed": 0,
            "post_release_remote_fact_count": 0,
        },
        actions={"due_lifecycle_mismatch_count": 0, "due_open_count": 0},
        attempts={"post_release_count": 0},
    )

    assert blockers == ["task_not_running"]
    assert module._goal_status(task, blockers) == "paused"

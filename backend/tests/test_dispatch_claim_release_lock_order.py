from __future__ import annotations

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Task
from app.services._common import _now
from app.services.task_center import dispatcher
from test_dispatch_claim_reservations import _seed_strict_actions, _settings

pytestmark = pytest.mark.no_postgres


def test_release_locks_scope_before_flushing_dirty_business_rows(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(
        dispatcher,
        "get_settings",
        lambda: _settings(dispatcher_concurrency=1),
    )
    statements: list[str] = []
    with Session(engine) as session:
        _seed_strict_actions(session, _now().replace(second=0, microsecond=0))
        action = dispatcher.claim_actions(session, limit=1, worker_id="lock-order")[0]
        task = session.get(Task, action.task_id)
        event.listen(
            engine,
            "before_cursor_execute",
            lambda _conn, _cursor, statement, *_args: statements.append(statement),
        )
        task.name = "dirty-before-release"
        action.status = "success"

        assert dispatcher.release_dispatch_claim(session, action) is True
        session.flush()

    scope_lock = _statement_index(statements, "FROM dispatch_claim_scopes")
    task_flush = _statement_index(statements, "UPDATE tasks SET")
    assert scope_lock < task_flush


def _statement_index(statements: list[str], marker: str) -> int:
    return next(index for index, statement in enumerate(statements) if marker in statement)

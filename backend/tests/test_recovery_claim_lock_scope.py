from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy.dialects import postgresql

from app.models import Action
from app.services.task_center.recovery_claims import claim_recovery_actions


pytestmark = pytest.mark.no_postgres


def test_postgres_recovery_claim_locks_only_action_rows() -> None:
    statements = []

    class FakeSession:
        bind = type("Bind", (), {"dialect": type("Dialect", (), {"name": "postgresql"})()})()

        @staticmethod
        def scalars(statement):
            statements.append(statement)
            return []

        @staticmethod
        def commit() -> None:
            return None

    claim_recovery_actions(
        FakeSession(),
        conditions=(Action.status == "executing",),
        order_by=(Action.scheduled_at.asc(), Action.id.asc()),
        now=datetime(2026, 7, 13, 23, 30),
        limit=20,
    )
    sql = str(statements[0].compile(dialect=postgresql.dialect()))

    assert "FOR UPDATE OF actions SKIP LOCKED" in sql

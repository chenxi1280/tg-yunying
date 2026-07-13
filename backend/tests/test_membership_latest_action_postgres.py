from __future__ import annotations

from datetime import datetime, timedelta
from time import perf_counter

import pytest
from sqlalchemy import delete, event

from app.database import Base, SessionLocal, engine
from app.models import Action, Task, Tenant, TgAccount
from app.services._common import _now
from app.services.task_center.channel_membership import _membership_actions_by_account


TEST_TENANT_ID = 913_714
TEST_ACCOUNT_ID_BASE = 2_000_000
TEST_ACCOUNT_COUNT = 580
TEST_HISTORY_PER_ACCOUNT = 4
TEST_CHANNEL_ID = 902_714
MAX_QUERY_SECONDS = 5.0
LARGE_JSON_TEXT = "membership-history" * 256


def _seed_membership_history() -> None:
    now_value = _now()
    with SessionLocal() as session:
        session.add(Tenant(id=TEST_TENANT_ID, name="membership latest action postgres"))
        session.commit()
        session.add(
            Task(
                id="pg-membership-latest",
                tenant_id=TEST_TENANT_ID,
                name="PG latest membership",
                type="group_ai_chat",
                status="running",
            )
        )
        session.add_all(
            TgAccount(
                id=TEST_ACCOUNT_ID_BASE + index,
                tenant_id=TEST_TENANT_ID,
                display_name=f"PG账号{index}",
                phone_masked=str(index),
                status="在线",
            )
            for index in range(TEST_ACCOUNT_COUNT)
        )
        session.flush()
        session.add_all(_history_actions(now_value))
        session.commit()


def _cleanup_membership_history() -> None:
    with SessionLocal() as session:
        session.execute(delete(Action).where(Action.tenant_id == TEST_TENANT_ID))
        session.execute(delete(Task).where(Task.tenant_id == TEST_TENANT_ID))
        session.execute(delete(TgAccount).where(TgAccount.tenant_id == TEST_TENANT_ID))
        session.execute(delete(Tenant).where(Tenant.id == TEST_TENANT_ID))
        session.commit()


def _history_actions(now_value: datetime):
    for index in range(TEST_ACCOUNT_COUNT):
        account_id = TEST_ACCOUNT_ID_BASE + index
        for history_index in range(TEST_HISTORY_PER_ACCOUNT):
            yield Action(
                id=f"pg-membership-{index:04d}-{history_index}",
                tenant_id=TEST_TENANT_ID,
                task_id="pg-membership-latest",
                task_type="group_ai_chat",
                action_type="ensure_target_membership",
                account_id=account_id,
                status="success",
                created_at=now_value + timedelta(seconds=history_index),
                payload={"channel_target_id": TEST_CHANNEL_ID, "large": LARGE_JSON_TEXT},
                result={"large": LARGE_JSON_TEXT},
            )


@pytest.mark.allow_missing_rule_binding
def test_membership_latest_action_postgres_scales_by_account_not_history() -> None:
    Base.metadata.create_all(engine)
    _cleanup_membership_history()
    statements: list[str] = []

    def track_select(_connection, _cursor, statement, _parameters, _context, _executemany):
        if "row_number" in statement.lower() and "from actions" in statement.lower():
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", track_select)
    try:
        _seed_membership_history()
        with SessionLocal() as session:
            started_at = perf_counter()
            latest = _membership_actions_by_account(
                session,
                TEST_CHANNEL_ID,
                task_id="pg-membership-latest",
            )
            elapsed = perf_counter() - started_at
    finally:
        event.remove(engine, "before_cursor_execute", track_select)
        _cleanup_membership_history()

    assert len(latest) == TEST_ACCOUNT_COUNT
    assert all(action.id.endswith(f"-{TEST_HISTORY_PER_ACCOUNT - 1}") for action in latest.values())
    assert elapsed < MAX_QUERY_SECONDS
    assert len(statements) == 1
    assert "row_number() over" in statements[0].lower()

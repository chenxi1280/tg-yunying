"""Batch freezing preserves native owner rules and avoids per-owner SELECTs."""
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from app.models import Action, ChannelMessage, OperationTarget, Task, TaskDayLedger, Tenant, TgAccount, ViewFulfillmentObligation
from app.services.task_center.channel_view_obligation_batch import ensure_view_obligations
from app.services.task_center.engagement_runtime_error import RuntimeResourceBlocked
from tests.owner_postgres_support import owner_engine

pytestmark = pytest.mark.isolated_postgres


def seed(session, count=40):
    session.add(Tenant(id=1, name="batch"))
    session.flush()
    task = Task(tenant_id=1, name="view", type="channel_view", status="running")
    target = OperationTarget(tenant_id=1, target_type="channel", tg_peer_id="-100101", title="batch")
    session.add_all([task, target])
    session.add_all(TgAccount(id=i, tenant_id=1, display_name=str(i), phone_masked=str(i))
        for i in range(1, count + 1))
    session.flush()
    message = ChannelMessage(tenant_id=1, channel_target_id=target.id, message_id=1)
    now = datetime(2026, 9, 11, tzinfo=timezone.utc)
    ledger = TaskDayLedger(tenant_id=1, task_id=task.id, timezone_snapshot="Asia/Shanghai",
        timezone_revision=1, obligation_local_date=date(2026, 9, 11), period_start_at=now,
        deadline_at=now + timedelta(days=1), planning_anchor_at=now, day_phase="full_day_committed")
    session.add_all([message, ledger])
    session.flush()
    return task, ledger, message


def test_batch_preserves_terminal_unknown_confirmed_and_ordinals(owner_engine):
    with Session(owner_engine) as session:
        task, ledger, message = seed(session)
        statuses = ("failed", "unknown_after_send", "executing", "failed", "failed")
        actions = [Action(tenant_id=1, task_id=task.id, task_type="channel_view",
            action_type="view_message", account_id=i, status=status)
            for i, status in enumerate(statuses, 1)]
        session.add_all(actions)
        session.flush()
        old = [ViewFulfillmentObligation(tenant_id=1, task_day_ledger_id=ledger.id,
            channel_message_id=message.id, account_id=i, current_action_id=action.id,
            status="confirmed" if i == 4 else "pending", pacing_slot_ordinal=i)
            for i, action in enumerate(actions, 1)]
        session.add_all(old)
        session.flush()
        actions[4].payload = {"view_fulfillment_obligation_id": "other-owner"}
        session.flush()
        queries = []
        def observe(conn, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith("SELECT"):
                queries.append(statement)
        event.listen(session.connection(), "before_cursor_execute", observe)
        request = [(message, i) for i in range(1, 41)] + [(message, 1)]
        owners = ensure_view_obligations(session, task, ledger=ledger, actions=request)
        assert len(queries) == 2
        assert len(owners) == 40
        assert [owners[(message.id, i)].current_action_id for i in range(1, 6)] == [
            None, actions[1].id, actions[2].id, actions[3].id, None]
        assert [owners[(message.id, i)].pacing_slot_ordinal for i in range(1, 6)] == list(range(1, 6))
        queries.clear()
        repeated = ensure_view_obligations(session, task, ledger=ledger, actions=request)
        assert len(queries) == 2
        assert {row.id for row in owners.values()} == {row.id for row in repeated.values()}
        event.remove(session.connection(), "before_cursor_execute", observe)


def test_new_owner_qualification_is_still_required(owner_engine):
    with Session(owner_engine) as session:
        task, ledger, message = seed(session, count=1)
        task.type_config = {"engagement_contract_version": "unified_engagement_v1"}
        session.flush()
        with pytest.raises(RuntimeResourceBlocked):
            ensure_view_obligations(session, task, ledger=ledger, actions=[(message, 1)])
        assert session.scalar(select(ViewFulfillmentObligation.id)) is None

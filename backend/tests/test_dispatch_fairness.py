from __future__ import annotations

import pytest
from datetime import timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Action, DispatchFairnessCursor, Task, Tenant, TgAccount
from app.services._common import _now
from app.services.task_center import dispatcher
from app.services.task_center.dispatch_fairness import (
    classify_action_payload,
    record_claim_class,
    should_prefer_ordinary_after_hard_hourly,
)

pytestmark = pytest.mark.no_postgres


def test_prefer_ordinary_after_hard_hourly_claim():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Tenant(id=1, name="t"))
        record_claim_class(session, tenant_id=1, claimed_class="hard_hourly")
        session.commit()
        decision = should_prefer_ordinary_after_hard_hourly(
            session,
            tenant_id=1,
            has_due_ordinary=True,
            has_due_hard_hourly=True,
        )
        assert decision.preferred_class == "ordinary"
        assert decision.reason == "hard_hourly_then_ordinary"
        cursor = session.scalar(select(DispatchFairnessCursor).where(DispatchFairnessCursor.tenant_id == 1))
        # Read-only decision must not mutate cursor.
        assert cursor and cursor.last_claim_class == "hard_hourly"


def test_classify_hard_hourly_and_search():
    assert classify_action_payload("send_message", {"hard_hourly_target": True}) == "hard_hourly"
    assert classify_action_payload("search_join", {}) == "search_join"
    assert classify_action_payload("send_message", {}) == "ordinary"


def test_claim_fairness_orders_ordinary_before_sql_limit_after_hard_hourly():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()
    with Session(engine) as session:
        session.add(Tenant(id=1, name="t"))
        session.add_all(
            [
                TgAccount(id=11, tenant_id=1, display_name="硬目标账号", phone_masked="+861***0011", status="在线"),
                TgAccount(id=12, tenant_id=1, display_name="普通账号", phone_masked="+861***0012", status="在线"),
            ]
        )
        session.add_all(
            [
                Task(id="hard-task", tenant_id=1, name="硬目标", type="group_ai_chat", status="running", priority=1),
                Task(id="ordinary-task", tenant_id=1, name="普通任务", type="group_ai_chat", status="running", priority=9),
            ]
        )
        session.add_all(
            [
                Action(
                    id="hard-action",
                    tenant_id=1,
                    task_id="hard-task",
                    task_type="group_ai_chat",
                    action_type="send_message",
                    account_id=11,
                    status="pending",
                    scheduled_at=now_value - timedelta(minutes=1),
                    payload={"hard_hourly_target": True},
                ),
                Action(
                    id="ordinary-action",
                    tenant_id=1,
                    task_id="ordinary-task",
                    task_type="group_ai_chat",
                    action_type="send_message",
                    account_id=12,
                    status="pending",
                    scheduled_at=now_value - timedelta(minutes=1),
                    payload={},
                ),
            ]
        )
        record_claim_class(session, tenant_id=1, claimed_class="hard_hourly")
        session.commit()

        claimed = dispatcher.claim_actions(session, limit=1, worker_id="fairness-test")
        try:
            assert [action.id for action in claimed] == ["ordinary-action"]
            cursor = session.scalar(select(DispatchFairnessCursor).where(DispatchFairnessCursor.tenant_id == 1))
            # Cursor advances only after successful claim.
            assert cursor and cursor.last_claim_class == "ordinary"
        finally:
            for action in claimed:
                dispatcher._release_runtime_resources(action)


def test_failed_empty_claim_does_not_advance_fairness_cursor():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Tenant(id=1, name="t"))
        record_claim_class(session, tenant_id=1, claimed_class="hard_hourly")
        session.commit()
        claimed = dispatcher.claim_actions(session, limit=1, worker_id="empty-claim")
        assert claimed == []
        cursor = session.scalar(select(DispatchFairnessCursor).where(DispatchFairnessCursor.tenant_id == 1))
        assert cursor and cursor.last_claim_class == "hard_hourly"


def test_claim_does_not_write_cursor_before_confirm(monkeypatch: pytest.MonkeyPatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()
    observed: dict[str, str] = {}

    with Session(engine) as session:
        session.add(Tenant(id=1, name="t"))
        session.add_all(
            [
                TgAccount(id=11, tenant_id=1, display_name="硬目标账号", phone_masked="+861***0011", status="在线"),
                TgAccount(id=12, tenant_id=1, display_name="普通账号", phone_masked="+861***0012", status="在线"),
                Task(id="hard-task", tenant_id=1, name="硬目标", type="group_ai_chat", status="running", priority=1),
                Task(id="ordinary-task", tenant_id=1, name="普通任务", type="group_ai_chat", status="running", priority=9),
                Action(
                    id="hard-action",
                    tenant_id=1,
                    task_id="hard-task",
                    task_type="group_ai_chat",
                    action_type="send_message",
                    account_id=11,
                    status="pending",
                    scheduled_at=now_value - timedelta(minutes=1),
                    payload={"hard_hourly_target": True},
                ),
                Action(
                    id="ordinary-action",
                    tenant_id=1,
                    task_id="ordinary-task",
                    task_type="group_ai_chat",
                    action_type="send_message",
                    account_id=12,
                    status="pending",
                    scheduled_at=now_value - timedelta(minutes=1),
                    payload={},
                ),
            ]
        )
        record_claim_class(session, tenant_id=1, claimed_class="hard_hourly")
        session.commit()

        original = dispatcher._claimable_candidates

        def capture_cursor(candidates):
            cursor = session.scalar(select(DispatchFairnessCursor).where(DispatchFairnessCursor.tenant_id == 1))
            observed["last_claim_class"] = cursor.last_claim_class if cursor else ""
            return original(candidates)

        monkeypatch.setattr(dispatcher, "_claimable_candidates", capture_cursor)
        claimed = dispatcher.claim_actions(session, limit=1, worker_id="fairness-after-confirm-test")
        try:
            assert [action.id for action in claimed] == ["ordinary-action"]
            # Before confirm path finishes, selection-time capture still sees prior hard_hourly.
            assert observed["last_claim_class"] == "hard_hourly"
            cursor = session.scalar(select(DispatchFairnessCursor).where(DispatchFairnessCursor.tenant_id == 1))
            assert cursor and cursor.last_claim_class == "ordinary"
        finally:
            for action in claimed:
                dispatcher._release_runtime_resources(action)

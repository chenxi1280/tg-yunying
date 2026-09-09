"""Collector must remain claimable while listener discovers other routes."""
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.models import (
    Task, TelegramAuthorizationUpdateState, Tenant, TgAccount,
    TgAccountAuthorization, TgGroup,
)
from app.services.task_center import listener_runtime as listener
from app.services.task_center.telegram_update_collector import (
    CollectorDrainResult, _claim_state,
)
from tests import test_runtime_retention_protection_postgres as postgres_fixtures

pytestmark = [pytest.mark.isolated_postgres, pytest.mark.allow_missing_rule_binding]
database = postgres_fixtures.database


def _seed(session):
    from app.services.task_center.group_ai_update_stream import ensure_group_ai_update_subscription

    session.add(Tenant(id=1, name="collector-lock-test"))
    session.flush()
    account = TgAccount(id=11, tenant_id=1, display_name="listener", phone_masked="11",
                        status="在线", authorization_generation=1)
    session.add(account)
    session.flush()
    authorization = TgAccountAuthorization(id=21, tenant_id=1, account_id=11,
        slot_generation=1, is_current=True, is_slot_current=True,
        status="active", session_ciphertext="test-session")
    session.add(authorization)
    session.flush()
    account.current_authorization_id = authorization.id
    group = TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="source", auth_status="已授权运营")
    task = Task(id="collector-lock-task", tenant_id=1, name="AI stream",
                type="group_ai_chat", status="running",
                type_config={"engagement_contract_version": "unified_engagement_v1"})
    session.add_all([group, task])
    session.flush()
    assert ensure_group_ai_update_subscription(session, task, group, listener_account_id=11)
    state = session.scalar(select(TelegramAuthorizationUpdateState))
    state.state = "live"
    state.common_pts = 1
    state.common_date = 1
    return task, group, state


def _runtime(monkeypatch, database):
    with Session(database) as session:
        task, group, state = _seed(session)
        state.owner_id = ""
        state.lease_expires_at = None
        task_id, state_id = task.id, state.id
        source = listener.ListenerRuntimeSource(
            group_id=group.id, tenant_id=1, task_ids=[task.id],
            account_ids=[11], task_account_ids={task.id: [11]},
        )
        session.commit()
    monkeypatch.setattr(listener, "_listener_sources", lambda *a, **k: [source])
    monkeypatch.setattr(listener, "_drain_listener_source", lambda *a: None)
    monkeypatch.setattr(listener, "drain_channel_listener_runtime",
                        lambda *a, **k: listener.ListenerRuntimeDrainResult())
    return sessionmaker(bind=database), task_id, state_id


def test_comment_discovery_does_not_hold_shared_collector_lock(database, monkeypatch):
    factory, task_id, state_id = _runtime(monkeypatch, database)
    stages = []

    def discover(*args, **kwargs):
        with factory() as collector:
            collector.scalar(select(TelegramAuthorizationUpdateState).where(
                TelegramAuthorizationUpdateState.id == state_id,
            ).with_for_update(nowait=True))
        assert _claim_state(factory, state_id) is not None
        stages.append("discovery")
        return []

    def collect(*args, **kwargs):
        with factory() as readback:
            task = readback.get(Task, task_id)
            assert task.stats["group_update_stream_state"] == "live"
        stages.append("collector")
        return CollectorDrainResult()

    monkeypatch.setattr(listener, "_channel_comment_stream_bindings", discover)
    monkeypatch.setattr(listener, "drain_telegram_update_collector", collect)
    listener.drain_listener_runtime(factory, tenant_id=1, limit=1)
    assert stages == ["discovery", "collector"]


def test_failed_route_discovery_does_not_begin_subscription_setup(database, monkeypatch):
    factory, _, _ = _runtime(monkeypatch, database)
    writes = []
    monkeypatch.setattr(listener, "_ensure_group_ai_streams",
                        lambda *args: writes.append("subscription_setup") or 0)

    def fail(*args, **kwargs):
        raise RuntimeError("route_discovery_failed")

    monkeypatch.setattr(listener, "_channel_comment_stream_bindings", fail)
    with pytest.raises(RuntimeError, match="route_discovery_failed"):
        listener.drain_listener_runtime(factory, tenant_id=1, limit=1)
    assert writes == []

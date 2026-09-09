"""Real source-row locking and window intersections for AI gap allocation."""
import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event

import pytest
from sqlalchemy import select

from app.models import (
    Action, ExecutionAttempt, OperationTarget, SourcePacingAdmission,
    SourcePacingState, Tenant, TgAccount,
)
from app.services.task_center.source_pacing import wall_datetime
from app.services.task_center.source_pacing_admission import admit_source_paced_attempt
from tests.postgres_pacing_e4_fixture import factory as factory
from tests.test_source_pacing_gap import GAP, _windows
from tests.test_source_pacing_owner_reuse import (
    ACCOUNT_ID, NOW, TARGET_ID, TENANT_ID, _action, _attempt, _owner_entities,
)


pytestmark = [pytest.mark.isolated_postgres, pytest.mark.allow_missing_rule_binding]


def _seed(session):
    session.add(Tenant(id=TENANT_ID, name="source gap PG"))
    session.flush()
    session.add(TgAccount(id=ACCOUNT_ID, tenant_id=TENANT_ID,
                          display_name="source gap", phone_masked="***"))
    session.add(OperationTarget(id=TARGET_ID, tenant_id=TENANT_ID, target_type="group",
                                tg_peer_id="-1009001", title="source gap"))
    session.flush()
    for suffix in ("one", "two"):
        task, ledger, slot = _owner_entities(f"task-{suffix}", f"slot-{suffix}", NOW)
        for item in (task, ledger, slot):
            session.add(item)
            session.flush()
        action = _action(f"action-{suffix}", task.id, slot.id, NOW)
        session.add(action)
        session.flush()
        session.add(_attempt(action, 1, NOW))
        session.flush()
    session.add(SourcePacingState(
        tenant_id=TENANT_ID, pacing_domain="ai_send",
        source_key_hash=hashlib.sha256(b"-1009001").hexdigest(),
        next_call_not_before_at=NOW + timedelta(days=3),
    ))
    session.commit()


def _admit(session, suffix):
    action = session.get(Action, f"action-{suffix}")
    attempt = session.scalar(select(ExecutionAttempt).where(ExecutionAttempt.action_id == action.id))
    return admit_source_paced_attempt(session, action, attempt, now_value=NOW)


def test_two_connections_keep_source_lock_and_allocate_distinct_times(factory):
    with factory() as seed:
        _seed(seed)
    with factory() as owner, factory() as observer:
        assert _admit(owner, "one")
        state = owner.scalar(select(SourcePacingState))
        # The existing source row remains locked until the allocation commits.
        assert observer.scalar(select(SourcePacingState.id).where(
            SourcePacingState.id == state.id).with_for_update(skip_locked=True)) is None
        entered = Event()
        with ThreadPoolExecutor(max_workers=1) as pool:
            contender = pool.submit(_compete, factory, entered)
            assert entered.wait(timeout=1)
            owner.commit()
            assert contender.result(timeout=5) == (False, NOW + GAP)
    with factory() as readback:
        rows = list(readback.scalars(select(SourcePacingAdmission).order_by(
            SourcePacingAdmission.call_not_before_at)))
        assert [wall_datetime(row.call_not_before_at) for row in rows] == [NOW, NOW + GAP]
        assert [row.state for row in rows] == ["call_started", "reserved"]


def _compete(factory, entered):
    with factory() as session:
        entered.set()
        allowed = _admit(session, "two")
        action = session.get(Action, "action-two")
        at = wall_datetime(action.scheduled_at)
        session.commit()
        return allowed, at


def test_postgres_source_shift_uses_original_half_open_window(factory):
    with factory() as session:
        _seed(session)
        action = session.get(Action, "action-two")
        _windows(session, action, [(NOW, NOW + GAP), (NOW + GAP * 3, NOW + GAP * 5)])
        assert _admit(session, "one")
        assert not _admit(session, "two")
        session.commit()
    with factory() as readback:
        action = readback.get(Action, "action-two")
        assert wall_datetime(action.scheduled_at) == NOW + GAP * 3
        assert wall_datetime(action.pacing_due_at) == NOW

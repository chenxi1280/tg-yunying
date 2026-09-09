from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    Action, ExecutionAttempt, FulfillmentRemoteFact, Task, TaskAccountDailyCoverage,
    TaskDayLedger, TaskGroupDailyMessageSlot, TgAccount,
)
from tests.test_task_fulfillment_e4_diagnostics import load_module


pytestmark = pytest.mark.no_postgres
SINCE = datetime(2026, 9, 9, 12)
OBSERVED = SINCE + timedelta(minutes=1)


@pytest.fixture
def scope():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        task = Task(id="task", tenant_id=1, name="diagnostic", type="group_ai_chat", status="running",
            type_config={"engagement_contract_version": "unified_engagement_v1"})
        ledger = TaskDayLedger(id="ledger", tenant_id=1, task_id=task.id,
            timezone_snapshot="Asia/Shanghai", timezone_revision=1,
            obligation_local_date=date(2026, 9, 9), period_start_at=SINCE,
            deadline_at=SINCE + timedelta(days=1), day_phase="full_day", planning_anchor_at=SINCE)
        session.add_all([task, ledger])
        session.flush()
        yield session, task, ledger
    engine.dispose()


def _message(session, *, suffix="one", **overrides):
    slot = TaskGroupDailyMessageSlot(id=f"slot-{suffix}", tenant_id=1, task_id="task",
        task_day_ledger_id="ledger", target_operation_target_id=1,
        slot_kind="extra", slot_ordinal=len(session.new) + 1)
    action = Action(id=f"action-{suffix}", tenant_id=1, task_id="task", account_id=1,
        task_type="group_ai_chat", action_type="send_message", scheduled_at=SINCE,
        status="success", primary_quantity_slot_id=slot.id, payload={})
    attempt = ExecutionAttempt(id=f"attempt-{suffix}", tenant_id=1,
        action_id=action.id, account_id=1, status="success", remote_message_id="123",
        gateway_call_started_at=SINCE, after_call_at=OBSERVED)
    values = dict(fact_id=f"fact-{suffix}", tenant_id=1, task_id="task", task_type="group_ai_chat",
        task_day_ledger_id="ledger", obligation_type="test", obligation_id=suffix,
        action_id=action.id, attempt_id=attempt.id, mutation_kind="send_message",
        remote_mutation_key_hash=suffix, gateway_request_hash=suffix,
        fact_kind="remote_message_observed", fact_identity_hash=suffix, observed_at=OBSERVED,
        outcome={"remote_message_id": "123"})
    fact = FulfillmentRemoteFact(**{**values, **overrides})
    session.add_all([slot, action, attempt, fact])
    session.flush()
    return action, attempt, fact


def _coverage(session, *, suffix, state="unknown", account_id=1, **overrides):
    values = dict(id=suffix, tenant_id=1, task_id="task", task_day_ledger_id="ledger",
        group_id=len(suffix), account_id=account_id, coverage_date=date(2026, 9, 9),
        target_count=1, confirmed_count=0, state=state, last_remote_message_id="")
    session.add(TaskAccountDailyCoverage(**{**values, **overrides}))
    session.flush()


def test_success_receipt_without_typed_fact_does_not_pass_e4(scope):
    session, task, ledger = scope
    _, _, fact = _message(session)
    session.delete(fact)
    session.flush()
    module = load_module()
    snapshot = module.task_snapshot(session, task.id, SINCE)
    assert snapshot["attempts"]["post_release_remote_success_count"] == 1
    assert "ai_post_release_remote_fact_missing" in module.e4_blockers(snapshot)


def test_valid_fact_counts_once_per_action_and_queries_are_read_only(scope):
    session, _, ledger = scope
    action, attempt, _ = _message(session)
    duplicate = FulfillmentRemoteFact(fact_id="repeat", tenant_id=1, task_id="task",
        task_type="group_ai_chat", task_day_ledger_id="ledger", obligation_type="test",
        obligation_id="repeat", action_id=action.id, attempt_id=attempt.id,
        mutation_kind="send_message", remote_mutation_key_hash="repeat", gateway_request_hash="repeat",
        fact_kind="remote_message_observed", fact_identity_hash="repeat", observed_at=OBSERVED,
        outcome={"remote_message_id": "123"})
    session.add(duplicate)
    session.flush()
    statements = []
    event.listen(session.bind, "before_cursor_execute", lambda conn, cursor, sql, *args: statements.append(sql))
    result = load_module()._group_daily_snapshot(session, ledger, since=SINCE)
    assert result["post_release_remote_fact_count"] == 1
    assert all(sql.lstrip().upper().startswith("SELECT") for sql in statements)
    assert not session.dirty and not session.new and not session.deleted


@pytest.mark.parametrize("changes", [
    {"fact_kind": "membership_observed"}, {"fact_kind": "remote_outcome_unknown"},
    {"task_day_ledger_id": "old-ledger"}, {"tenant_id": 2}, {"task_id": "other-task"},
    {"task_type": "channel_comment"}, {"mutation_kind": "ensure_target_membership"},
    {"attempt_id": "missing"}, {"action_id": "missing"},
    {"observed_at": SINCE - timedelta(seconds=1)},
    {"outcome": {"remote_message_id": "other"}}, {"outcome": {}},
])
def test_wrong_fact_identity_never_credits_current_task(scope, changes):
    session, _, ledger = scope
    _message(session, **changes)
    assert load_module()._group_daily_snapshot(session, ledger, since=SINCE)["post_release_remote_fact_count"] == 0


@pytest.mark.parametrize("changes", [
    {"status": "result_unknown"}, {"remote_message_id": ""}, {"account_id": 2},
    {"tenant_id": 2}, {"action_id": "other-action"},
    {"gateway_call_started_at": None}, {"gateway_call_started_at": SINCE - timedelta(seconds=1)},
    {"gateway_call_started_at": OBSERVED + timedelta(seconds=1)},
])
def test_fact_requires_original_successful_post_release_attempt(scope, changes):
    session, _, ledger = scope
    _, attempt, _ = _message(session)
    for key, value in changes.items():
        setattr(attempt, key, value)
    session.flush()
    assert load_module()._group_daily_snapshot(session, ledger, since=SINCE)["post_release_remote_fact_count"] == 0


@pytest.mark.parametrize("changes", [
    {"tenant_id": 2}, {"task_id": "other-task"}, {"task_type": "channel_comment"},
    {"action_type": "ensure_target_membership"}, {"account_id": None},
    {"payload": {"task_day_ledger_id": "other-ledger"}},
    {"primary_quantity_slot_id": None}, {"primary_quantity_slot_id": "missing"},
])
def test_fact_requires_matching_action_identity(scope, changes):
    session, _, ledger = scope
    action, _, _ = _message(session)
    for key, value in changes.items():
        setattr(action, key, value)
    session.flush()
    assert load_module()._group_daily_snapshot(session, ledger, since=SINCE)["post_release_remote_fact_count"] == 0


def test_missing_typed_fact_field_cannot_fall_back_to_receipt():
    module = load_module()
    snapshot = {"task_status": "running", "task_type": "group_ai_chat", "ledger_id": "ledger",
        "attempts": {"post_release_remote_success_count": 10},
        "group_daily": {"target_row_count": 1, "due_message_count": 0, "confirmed_message_count": 0}}
    assert module.e4_blockers(snapshot) == ["ai_post_release_remote_fact_missing"]


def test_account_status_does_not_erase_coverage_and_counts_are_distinct(scope):
    session, _, ledger = scope
    _coverage(session, suffix="a", state="abandoned_for_day")
    _coverage(session, suffix="bb", state="unknown")
    _coverage(session, suffix="ccc", state="pending_admission", account_id=2)
    _coverage(session, suffix="dddd", state="unknown")
    module = load_module()
    before = module._group_daily_snapshot(session, ledger)
    account = TgAccount(id=1, tenant_id=1, display_name="test", phone_masked="test",
        status="疑似封禁", telegram_frozen=True)
    session.add(account)
    session.flush()
    assert module._group_daily_snapshot(session, ledger) == before
    assert before["coverage_required_count"] == 4
    assert before["coverage_active_count"] == 3
    runtime = module._group_runtime_snapshot(session, ledger)
    assert runtime["coverage_distinct_account_count"] == 2
    unknown = next(row for row in runtime["coverage_counts"] if row["state"] == "unknown")
    assert unknown["count"] == 2 and unknown["distinct_account_count"] == 1


@pytest.mark.parametrize("state", ["abandoned_for_day", "unknown", "pending_admission", "ready"])
def test_nonconfirmed_coverage_does_not_credit_stale_success_fields(scope, state):
    session, _, ledger = scope
    _coverage(session, suffix="stale", state=state, confirmed_count=1, last_remote_message_id="123")
    assert load_module()._group_daily_snapshot(session, ledger)["coverage_confirmed_count"] == 0


@pytest.mark.parametrize("changes", [{"tenant_id": 2}, {"task_id": "other-task"}, {"task_day_ledger_id": "old"}])
def test_coverage_scope_excludes_other_owners(scope, changes):
    session, _, ledger = scope
    _coverage(session, suffix="outside", **changes)
    module = load_module()
    assert module._group_daily_snapshot(session, ledger)["coverage_required_count"] == 0
    assert module._group_runtime_snapshot(session, ledger)["coverage_distinct_account_count"] == 0


def test_canonical_slot_without_payload_day_is_in_open_queue(scope):
    session, _, ledger = scope
    action, _, _ = _message(session)
    action.status = "pending"
    session.flush()
    runtime = load_module()._group_runtime_snapshot(session, ledger)
    assert sum(runtime["open_action_counts"].values()) == 1
    assert runtime["oldest_open_action_samples"][0]["ledger_matches"] is True


@pytest.mark.parametrize("changes", [
    {"tenant_id": 2}, {"task_id": "other"}, {"task_day_ledger_id": "old"},
])
def test_wrong_canonical_slot_cannot_use_matching_payload_as_fallback(scope, changes):
    session, _, ledger = scope
    action, _, _ = _message(session)
    action.payload = {"task_day_ledger_id": ledger.id}
    slot = session.get(TaskGroupDailyMessageSlot, action.primary_quantity_slot_id)
    for key, value in changes.items():
        setattr(slot, key, value)
    session.flush()
    assert load_module()._group_daily_snapshot(session, ledger, since=SINCE)["post_release_remote_fact_count"] == 0


def test_legacy_without_primary_slot_keeps_payload_ledger_contract(scope):
    session, task, ledger = scope
    task.type_config = {}
    action, _, _ = _message(session)
    action.primary_quantity_slot_id = None
    action.payload = {"task_day_ledger_id": ledger.id}
    session.flush()
    assert load_module()._group_daily_snapshot(session, ledger, since=SINCE)["post_release_remote_fact_count"] == 1

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import event

from app.models import (AccountBehaviorBudgetLedger, AccountBehaviorBudgetReservation,
    GatewayRequestEvidenceJournal, TaskDayLedger)
from app.services.task_center.engagement_legacy_occupancy import (
    LegacyOccupancyScope, read_legacy_attempt_occupancy)
from tests.test_engagement_runtime_resources import _attempt, _seed, _session


pytestmark = pytest.mark.no_postgres
DAY = date(2026, 9, 5)
SCOPE = LegacyOccupancyScope(tenant_id=1, account_ids=(11,), task_day=DAY)


def _call(session, task, *, when=None, state="success", action_type="like_message"):
    action, attempt = _attempt(session, task, 11, action_type=action_type)
    stamp = when or datetime(2026, 9, 5, 10)
    action.pacing_due_at = stamp
    attempt.gateway_call_started_at = stamp
    attempt.status = state
    attempt.after_call_at = stamp + timedelta(seconds=1)
    session.flush()
    return action, attempt


def _journal(session, action, attempt, *, state="true", suffix="", account_id=11):
    row = GatewayRequestEvidenceJournal(tenant_id=1, action_id=action.id,
        execution_attempt_id=attempt.id, account_id=account_id,
        gateway_request_identity=f"{attempt.id}{suffix}", request_fingerprint="r" * 64,
        target_fingerprint="t" * 64, result_fingerprint="s" * 64, evidence_hash="e" * 64,
        remote_mutation_state=state)
    session.add(row)
    session.flush()
    return row


def _ledger(session, task, day):
    start = datetime.combine(day, datetime.min.time())
    ledger = TaskDayLedger(tenant_id=1, task_id=task.id,
        timezone_snapshot="Asia/Shanghai", timezone_revision=1,
        obligation_local_date=day, period_start_at=start, deadline_at=start + timedelta(days=1),
        planning_anchor_at=start, day_phase="open")
    session.add(ledger)
    session.flush()
    return ledger


def test_call_date_does_not_move_old_frozen_fulfillment_day():
    with _session() as session:
        task = _seed(session)
        action, attempt = _call(session, task)
        action.pacing_due_at -= timedelta(days=1)
        session.flush()
        before = (action.pacing_due_at, attempt.gateway_call_started_at, dict(action.payload))
        row, = read_legacy_attempt_occupancy(session, SCOPE)
        assert row.original_task_day == DAY - timedelta(days=1)
        assert row.call_day == DAY
        assert not row.remote_inflight and not row.issues
        assert (action.pacing_due_at, attempt.gateway_call_started_at, action.payload) == before


def test_task_day_ledger_owns_date_even_when_pacing_is_different():
    with _session() as session:
        task = _seed(session)
        action, _ = _call(session, task)
        ledger = _ledger(session, task, DAY - timedelta(days=2))
        action.payload = {"task_day_ledger_id": ledger.id}
        session.flush()
        row, = read_legacy_attempt_occupancy(session, SCOPE)
        assert row.original_task_day == ledger.obligation_local_date
        assert row.call_day == DAY


@pytest.mark.parametrize("ack", [False, True])
def test_closed_unknown_and_transport_ack_keep_original_business_occupancy(ack):
    with _session() as session:
        task = _seed(session)
        action, attempt = _call(session, task, state="result_unknown")
        action.status = "closed_unknown"
        attempt.result_snapshot = {"transport_termination_state": "acknowledged" if ack else "unproven"}
        _journal(session, action, attempt, state="unknown")
        _journal(session, action, attempt, state="unknown", suffix="-deadline")
        row, = read_legacy_attempt_occupancy(session, SCOPE)
        assert row.remote_inflight is not ack
        assert row.original_task_day == row.call_day == DAY
        assert attempt.status == "result_unknown" and action.status == "closed_unknown"


def test_old_unacknowledged_call_is_still_reported_for_physical_capacity():
    with _session() as session:
        task = _seed(session)
        action, attempt = _call(session, task, state="result_unknown", when=datetime(2026, 8, 1, 1))
        action.status = "skipped"
        attempt.after_call_at = datetime(2026, 8, 1, 2)
        session.flush()
        row, = read_legacy_attempt_occupancy(session, SCOPE)
        assert row.remote_inflight and row.call_day == date(2026, 8, 1)
        attempt.result_snapshot = {"transport_termination_state": "acknowledged"}
        session.flush()
        assert read_legacy_attempt_occupancy(session, SCOPE) == ()


@pytest.mark.parametrize("evidence", ["snapshot", "journal"])
def test_proven_nonexecution_does_not_consume_business_budget(evidence):
    with _session() as session:
        task = _seed(session)
        action, attempt = _call(session, task, state="failed")
        if evidence == "journal":
            _journal(session, action, attempt, state="false")
        else:
            attempt.result_snapshot = {"remote_mutation_started": False}
            session.flush()
        assert read_legacy_attempt_occupancy(session, SCOPE) == ()


def test_before_gateway_failed_attempt_is_not_an_external_call():
    with _session() as session:
        task = _seed(session)
        _, attempt = _call(session, task, state="skipped_before_gateway")
        attempt.gateway_call_started_at = None
        session.flush()
        assert read_legacy_attempt_occupancy(session, SCOPE) == ()


@pytest.mark.parametrize("journal_state", ["false", "true"])
def test_durable_journal_takes_precedence_over_earlier_attempt_snapshot(journal_state):
    with _session() as session:
        task = _seed(session)
        action, attempt = _call(session, task, state="failed")
        attempt.result_snapshot = {"remote_mutation_started": journal_state == "false"}
        _journal(session, action, attempt, state=journal_state)
        rows = read_legacy_attempt_occupancy(session, SCOPE)
        assert len(rows) == (1 if journal_state == "true" else 0)
        assert all(not row.issues for row in rows)


def test_nonterminal_snapshot_false_cannot_release_an_active_call():
    with _session() as session:
        task = _seed(session)
        _, attempt = _call(session, task, state="gateway_started")
        attempt.after_call_at = None
        attempt.result_snapshot = {"remote_mutation_started": False}
        session.flush()
        row, = read_legacy_attempt_occupancy(session, SCOPE)
        assert row.remote_inflight


def test_conflicted_journal_false_cannot_release_uncertain_call():
    with _session() as session:
        task = _seed(session)
        action, attempt = _call(session, task, state="failed")
        journal = _journal(session, action, attempt, state="false")
        journal.state = "conflict"
        session.flush()
        row, = read_legacy_attempt_occupancy(session, SCOPE)
        assert row.remote_inflight and "gateway_journal_evidence_conflict" in row.issues


@pytest.mark.parametrize("damage,issue", [
    ("ledger", "original_task_day_ledger_mismatch"),
    ("account", "attempt_action_account_mismatch"),
    ("epoch", "attempt_action_epoch_mismatch"),
    ("tenant", "attempt_action_tenant_mismatch"),
    ("journal", "gateway_journal_owner_mismatch"),
    ("conflict", "remote_mutation_evidence_conflict"),
])
def test_missing_or_conflicting_evidence_is_visible(damage, issue):
    with _session() as session:
        task = _seed(session)
        action, attempt = _call(session, task)
        _damage(session, action, attempt, damage)
        session.flush()
        row, = read_legacy_attempt_occupancy(session, SCOPE)
        assert issue in row.issues


def _damage(session, action, attempt, damage):
    if damage == "ledger":
        action.payload = {"task_day_ledger_id": "absent"}
    if damage == "account":
        action.account_id = 12
    if damage == "epoch":
        action.task_lifecycle_epoch += 1
    if damage == "tenant":
        action.tenant_id = 2
    if damage == "journal":
        _journal(session, action, attempt, account_id=12)
    if damage == "conflict":
        _journal(session, action, attempt, state="true")
        _journal(session, action, attempt, state="false", suffix="-conflict")


def test_unknown_without_call_or_original_date_is_not_given_current_date():
    with _session() as session:
        task = _seed(session)
        action, attempt = _call(session, task, state="result_unknown")
        action.pacing_due_at = attempt.gateway_call_started_at = None
        session.flush()
        row, = read_legacy_attempt_occupancy(session, SCOPE)
        assert row.original_task_day is row.call_day is None
        assert set(row.issues) == {"original_task_day_unproven", "actual_call_day_unproven"}


def test_missing_fulfillment_day_does_not_undo_proven_transport_termination():
    with _session() as session:
        task = _seed(session)
        action, attempt = _call(session, task, state="result_unknown")
        action.pacing_due_at = None
        attempt.result_snapshot = {"transport_termination_state": "acknowledged"}
        session.flush()
        row, = read_legacy_attempt_occupancy(session, SCOPE)
        assert not row.remote_inflight and row.original_task_day is None
        assert row.issues == ("original_task_day_unproven",)


def test_existing_budget_reservation_is_not_counted_again():
    with _session() as session:
        task = _seed(session)
        action, attempt = _call(session, task)
        ledger = AccountBehaviorBudgetLedger(tenant_id=1, account_id=11, task_day=DAY,
            policy_revision_id="policy-already-owned", counters={"reaction": {"confirmed": 1}})
        session.add(ledger)
        session.flush()
        session.add(AccountBehaviorBudgetReservation(ledger_id=ledger.id, task_id=task.id,
            action_id=action.id, attempt_id=attempt.id, action_class="reaction", state="confirmed"))
        session.flush()
        assert read_legacy_attempt_occupancy(session, SCOPE) == ()
        assert ledger.counters == {"reaction": {"confirmed": 1}}


@pytest.mark.parametrize("count", [1, 25])
def test_batch_uses_one_select_and_never_flushes_pending_mutations(count):
    with _session() as session:
        task = _seed(session)
        for _ in range(count):
            action, attempt = _call(session, task)
            _journal(session, action, attempt)
        task.name = "pending-local-change"
        statements = []

        def record(_connection, _cursor, statement, _params, _context, _many):
            statements.append(statement.split()[0].upper())

        connection = session.connection()
        event.listen(connection, "before_cursor_execute", record)
        try:
            rows = read_legacy_attempt_occupancy(session, SCOPE)
        finally:
            event.remove(connection, "before_cursor_execute", record)
        assert len(rows) == count and statements == ["SELECT"]
        assert task in session.dirty


def test_all_action_classes_share_account_scope_but_other_accounts_do_not():
    with _session() as session:
        task = _seed(session)
        for action_type in ("send_message", "post_comment", "view_message", "like_message"):
            _call(session, task, action_type=action_type)
        _, excluded = _call(session, task)
        excluded.account_id = 12
        session.flush()
        rows = read_legacy_attempt_occupancy(session, SCOPE)
        assert {row.action_class for row in rows} == {"authored_message", "authored_comment", "view", "reaction"}
        assert all(row.account_id == 11 for row in rows)

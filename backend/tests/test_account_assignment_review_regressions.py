"""Regression coverage for scope preservation and per-dispatch Attempt identity."""
import copy

import pytest

from app.models import AccountPool, Action, ExecutionAttempt, TaskMembershipAdmissionItem, TgAccount
from app.services.task_center import dispatcher
from app.services.task_center.account_scope import _scope_account_ids
from app.services.task_center.channel_membership import _task_membership_candidates
from app.services.task_center.engagement_runtime_error import RuntimeResourceBlocked
from tests.test_engagement_assignment_eligibility import NOW, _invalidate
from tests.test_engagement_participation import _seed, _session

pytestmark = pytest.mark.no_postgres


def _candidate_ids(session, task, source):
    if source == "coverage":
        return _scope_account_ids(session, task)
    if source == "supplied_coverage":
        return _scope_account_ids(session, task, eligible_ids=[11, 12, 13, 14])
    if source == "persisted":
        task.type = "group_ai_chat"
        task.type_config = {**task.type_config, "account_coverage_mode": "all_accounts_daily"}
        session.add_all(TaskMembershipAdmissionItem(tenant_id=1, task_id=task.id,
            account_id=identity, target_id=101) for identity in (11, 12, 13, 14))
        session.flush()
    return [account.id for account in _task_membership_candidates(session, task)]


@pytest.mark.parametrize("source", ["configured", "coverage", "supplied_coverage", "persisted"])
@pytest.mark.parametrize("failure", ["disabled_pool", "purpose_mismatch"])
def test_scope_keeps_pool_and_purpose_restrictions(source, failure):
    with _session() as session:
        task = _seed(session)
        if failure == "disabled_pool":
            session.get(AccountPool, 1).is_enabled = False
        else:
            session.get(TgAccount, 11).account_identity = "code_receiver"
        session.flush()
        candidates = _candidate_ids(session, task, source)
        assert 11 not in candidates
        assert candidates == ([] if failure == "disabled_pool" else [12, 13, 14])


@pytest.mark.parametrize("failure", ["frozen", "expired"])
def test_scope_filter_keeps_health_failure_counting(failure):
    with _session() as session:
        task = _seed(session)
        _invalidate(session, session.get(TgAccount, 11), failure)
        assert [a.id for a in _task_membership_candidates(session, task)] == [12, 13, 14]
        summary = task.stats["account_assignment_eligibility"]
        assert summary["candidate_count"] == 4
        assert summary["excluded_count"] == 1
        assert "11" in summary["excluded_accounts"]


def _dispatch_fixture(session, monkeypatch, old_status):
    task = _seed(session)
    account = session.get(TgAccount, 11)
    action = Action(tenant_id=1, task_id=task.id, task_type=task.type,
        action_type="view_message", account_id=11, status="pending", scheduled_at=NOW)
    session.add(action)
    session.flush()
    previous = dispatcher._begin_execution_attempt(session, action, account) if old_status else None
    if previous is not None:
        previous.status = old_status
        previous.result_snapshot = {"historical_evidence": "unchanged"}
        if old_status in {"failed", "success", "unknown"}:
            previous.gateway_call_started_at = NOW
    session.commit()
    monkeypatch.setattr(dispatcher, "_awaiting_legacy_review", lambda *_: False)
    monkeypatch.setattr(dispatcher, "_action_pre_dispatch_handled", lambda *_: False)
    monkeypatch.setattr(dispatcher, "_dispatch_account", lambda *_: account)
    monkeypatch.setattr(dispatcher, "validate_action_payload", lambda *_: None)
    monkeypatch.setattr(dispatcher, "_release_runtime_resources", lambda *_: None)
    return action, account, previous


def _dispatch(session, action):
    return dispatcher._dispatch_action(session, action,
        generation_dependencies=None, comment_generation_dependencies=None)


@pytest.mark.parametrize("old_status", [None, "failed", "success", "unknown", "before_call", "skipped_before_gateway"])
def test_pre_attempt_rejection_defers_action_without_rewriting_history(monkeypatch, old_status):
    with _session() as session:
        action, account, previous = _dispatch_fixture(session, monkeypatch, old_status)
        before = copy.deepcopy(previous.result_snapshot) if previous else None
        _invalidate(session, account, "authorization_invalid")
        monkeypatch.setattr(dispatcher, "_dispatch_validated_action",
            lambda current, item, _context: dispatcher._begin_execution_attempt(current, item, account))
        assert _dispatch(session, action) is True
        assert action.status == "pending"
        assert action.result["error_code"] == "authorization_invalid"
        assert session.query(ExecutionAttempt).count() == (1 if previous else 0)
        if previous:
            assert previous.status == old_status
            assert previous.result_snapshot == before


def test_new_before_call_attempt_is_settled_while_history_is_untouched(monkeypatch):
    with _session() as session:
        action, account, previous = _dispatch_fixture(session, monkeypatch, "failed")
        before = copy.deepcopy(previous.result_snapshot)

        def admit(current, item, _context):
            dispatcher._begin_execution_attempt(current, item, account)
            raise RuntimeResourceBlocked("account_eligibility_busy", "concurrent update")

        monkeypatch.setattr(dispatcher, "_dispatch_validated_action", admit)
        assert _dispatch(session, action) is True
        newest = dispatcher._latest_execution_attempt(session, action.id)
        assert newest.id != previous.id
        assert newest.status == "skipped_before_gateway"
        assert newest.gateway_call_started_at is None
        assert previous.status == "failed" and previous.result_snapshot == before


def test_new_called_attempt_cannot_be_settled_as_uncalled(monkeypatch):
    with _session() as session:
        action, account, previous = _dispatch_fixture(session, monkeypatch, "failed")

        def admit(current, item, _context):
            newest = dispatcher._begin_execution_attempt(current, item, account)
            newest.gateway_call_started_at = NOW
            newest.status = "gateway_call_started"
            raise RuntimeResourceBlocked("account_eligibility_busy", "late failure")

        monkeypatch.setattr(dispatcher, "_dispatch_validated_action", admit)
        with pytest.raises(RuntimeError, match="engagement_gateway_defer_requires_uncalled_attempt"):
            _dispatch(session, action)
        newest = dispatcher._latest_execution_attempt(session, action.id)
        assert newest.status == "gateway_call_started"
        assert previous.status == "failed"

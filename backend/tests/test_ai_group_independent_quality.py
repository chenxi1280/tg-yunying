from datetime import timedelta

import pytest

from app.models import TaskGroupDailyMessageSlot
from app.services.task_center.ai_group_independent_quality import independent_quality_summary
from tests.test_production_e4_reporting_integrity import scope, _message, SINCE


pytestmark = pytest.mark.no_postgres


def _independent(session, *, suffix="one", emergency=False):
    action, attempt, fact = _message(session, suffix=suffix)
    action.obligation_type, action.obligation_id = fact.obligation_type, fact.obligation_id
    action.payload = {"emergency_selection_id": "selected"} if emergency else {
        "ai_generation_context_mode": "topic_only"}
    session.flush()
    return action, attempt, fact


def test_quality_reports_emergency_and_topic_separately(scope):
    session, task, ledger = scope
    first, _, _ = _independent(session, emergency=True)
    session.get(TaskGroupDailyMessageSlot, first.primary_quantity_slot_id).slot_ordinal = 2
    session.flush()
    _independent(session, suffix="two")
    assert independent_quality_summary(session, task, ledger.obligation_local_date) == {
        "remote_emergency_count": 1, "remote_topic_only_count": 1}


@pytest.mark.parametrize("invalid", ["missing_fact", "unknown", "uncalled", "wrong_account",
    "wrong_tenant", "wrong_obligation", "old_fact", "empty_remote", "wrong_day", "wrong_kind"])
def test_quality_requires_same_day_original_typed_success(scope, invalid):
    session, task, ledger = scope
    action, attempt, fact = _independent(session)
    if invalid == "missing_fact":
        session.delete(fact)
    elif invalid == "unknown":
        attempt.status = "unknown"
    elif invalid == "uncalled":
        attempt.gateway_call_started_at = None
    elif invalid == "wrong_account":
        attempt.account_id = 2
    elif invalid == "wrong_tenant":
        attempt.tenant_id = 2
    elif invalid == "wrong_obligation":
        fact.obligation_id = "other"
    elif invalid == "old_fact":
        fact.observed_at = SINCE - timedelta(seconds=1)
    elif invalid == "empty_remote":
        attempt.remote_message_id = ""
    elif invalid == "wrong_day":
        ledger.obligation_local_date -= timedelta(days=1)
    else:
        fact.mutation_kind = "join_channel"
    session.flush()
    assert independent_quality_summary(session, task, SINCE.date()) == {
        "remote_emergency_count": 0, "remote_topic_only_count": 0}

from copy import deepcopy
from datetime import datetime

import pytest

from app.models import Action, ExecutionAttempt, FulfillmentObligationProjection, Task, TgGroup
from app.services.task_center.dispatcher import _record_group_rescue_result
from app.services.task_center.group_rescue import (
    GroupRescueResult, refresh_group_rescue_action, rescue_action_snapshot,
)
from app.services.task_center.service import _action_payload
from test_group_rescue import _session, _seed_rescue_target

pytestmark = pytest.mark.no_postgres


@pytest.mark.parametrize("state,expected", [
    ("closed_unknown", "closed_unknown"), ("unknown_after_send", "unknown_after_send"),
    ("skipped", "skipped"), ("failed", "invite_failed"), ("success", "invite_success"),
    ("pending", "pending"),
])
def test_projection_never_requeues_or_erases_rescue(state, expected):
    rescue = Action(id="rescue", status=state, result={
        "rescue_status": "pending", "error_code": "original_error", "remote_fact_id": "original-fact",
    })
    before = deepcopy(rescue.result)
    source = Action(id="source", result={"source_evidence": "kept"})
    status, detail = rescue_action_snapshot(rescue)
    _record_group_rescue_result(source, GroupRescueResult(status, detail, rescue))
    assert status == expected
    assert rescue.status == state
    assert rescue.result == before
    assert source.result["source_evidence"] == "kept"


def test_stale_pending_caller_cannot_change_terminal_action():
    rescue = Action(id="rescue", status="closed_unknown", result={"original_fact": "kept"})
    source = Action(id="source", result={})
    _record_group_rescue_result(source, GroupRescueResult("pending", "stale", rescue))
    assert rescue.status == "closed_unknown"
    assert rescue.result == {"original_fact": "kept"}
    assert source.result["group_rescue_status"] == "closed_unknown"


@pytest.mark.parametrize("closed_obligation", [True, False])
def test_refresh_preserves_called_unknown_even_when_current_action_is_skipped(closed_obligation):
    with _session() as session:
        _seed_rescue_target(session)
        rescue = Action(id="old-rescue", tenant_id=1, task_id="task-rescue", task_type="group_membership_admission",
            action_type="invite_group_account", account_id=99, status="skipped",
            obligation_type="membership", obligation_id="original-owner",
            payload={"group_id": 7}, result={"rescue_status": "pending", "error_code": "obligation_not_open"})
        session.add(rescue)
        session.add(ExecutionAttempt(id="old-attempt", action_id=rescue.id, tenant_id=1,
            account_id=99, attempt_no=1, status="result_unknown",
            gateway_call_started_at=datetime(2026, 9, 1), result_snapshot={"remote_mutation_started": None}))
        if closed_obligation:
            session.add(FulfillmentObligationProjection(tenant_id=1, task_id="task-rescue",
                obligation_type="membership", obligation_id="original-owner", work_lane="membership",
                state="closed_with_unknown_shortfall", active_action_id=rescue.id))
        session.flush()
        before = deepcopy(rescue.result)
        outcome = refresh_group_rescue_action(session, session.get(Task, "task-rescue"),
            session.get(TgGroup, 7), rescue, trigger_account_id=11,
            trigger_reason="config changed", operation_target_id=21)
        assert outcome.status == ("closed_unknown" if closed_obligation else "unknown_after_send")
        assert rescue.status == "skipped"
        assert rescue.result == before
        assert _action_payload(rescue)["result"]["rescue_status"] == ("closed_unknown" if closed_obligation else "skipped")
        assert rescue.result == before

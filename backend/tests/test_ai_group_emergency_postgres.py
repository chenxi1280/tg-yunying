"""Real row locking and old-publication CAS, using only an isolated schema."""
from datetime import timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.models import (
    Action, AiGroupEmergencySelection, GenerationJob, OperationTarget, Task,
    TaskDayLedger, TaskGroupDailyMessageSlot, Tenant, TgAccount, TgGroup,
)
from app.services._common import _now
from app.timezone import as_beijing
from app.services.task_center.ai_generation_commit import commit_generation_action
from app.services.task_center.ai_generation_state import GenerationAttemptStale
from app.services.task_center.ai_group_emergency import select_emergency_content, validate_emergency_selection
from app.services.task_center.ai_group_emergency_worker import drain_emergency_content
from app.services.task_center.payloads import SendMessagePayload
from tests.postgres_pacing_e4_fixture import factory as factory


pytestmark = [pytest.mark.isolated_postgres, pytest.mark.allow_missing_rule_binding]


def _seed(session):
    now = as_beijing(_now())
    session.add(Tenant(id=1, name="emergency test"))
    session.flush()
    session.add(Task(id="task", tenant_id=1, name="emergency", type="group_ai_chat", status="running",
        type_config={"target_group_id": 7, "engagement_contract_version": "unified_engagement_v1"}))
    session.add(TgAccount(id=11, tenant_id=1, display_name="test", phone_masked="test"))
    session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="test"))
    session.add(OperationTarget(id=71, tenant_id=1, target_type="group", tg_peer_id="-1007", title="test"))
    session.flush()
    session.add(TaskDayLedger(id="ledger", tenant_id=1, task_id="task", timezone_snapshot="Asia/Shanghai",
        timezone_revision=1, obligation_local_date=now.date(), day_phase="active", planning_anchor_at=now,
        period_start_at=(now - timedelta(hours=1)).astimezone(timezone.utc),
        deadline_at=(now + timedelta(hours=1)).astimezone(timezone.utc)))
    session.flush()
    session.add(TaskGroupDailyMessageSlot(id="slot", tenant_id=1, task_id="task", task_day_ledger_id="ledger",
        target_operation_target_id=71, slot_kind="quantity", slot_ordinal=1))
    session.add(GenerationJob(id="job", tenant_id=1, task_id="task", obligation_type="group_quantity_slot",
        obligation_id="slot", generation_sequence=1, context_snapshot_version=1, state="failed"))
    session.flush()
    session.add(Action(id="action", tenant_id=1, task_id="task", task_type="group_ai_chat", action_type="send_message",
        account_id=11, status="pending", primary_quantity_slot_id="slot", obligation_type="group_quantity_slot",
        obligation_id="slot", scheduled_at=now, payload={"group_id": 7, "chat_id": "-1007",
            "generation_job_id": "job", "primary_quantity_slot_id": "slot", "ai_generation_status": "emergency_pending",
            "ai_generation_attempt_id": "old-attempt", "ai_generation_claim_owner": "", "ai_generation_claim_token": ""},
        result={"error_code": "provider_route_exhausted", "generation_outcome": "emergency_pending"}))
    session.commit()


def test_selector_lock_keeps_one_publication_and_old_generation_cas_cannot_overwrite(factory):
    with factory() as session:
        _seed(session)
    with factory() as owner:
        task = owner.scalar(select(Task).where(Task.id == "task").with_for_update())
        action = owner.scalar(select(Action).where(Action.id == "action").with_for_update())
        assert select_emergency_content(owner, task, action)
        assert drain_emergency_content(factory, 20) == 0
        owner.commit()
    with factory() as late:
        action = late.get(Action, "action")
        action.payload = {**action.payload, "message_text": "late normal result"}
        request = SimpleNamespace(tenant_id=1, task_id="task", claim_owner="old-worker",
                                  claim_token="old-token", attempt_id="old-attempt")
        with pytest.raises(GenerationAttemptStale):
            commit_generation_action(late, request, action)
        late.rollback()
    with factory() as check:
        from app.services.task_center.fulfillment_remote_facts import ensure_action_obligation

        action = check.get(Action, "action")
        selected_version = action.materialization_version
        assert ensure_action_obligation(check, action)
        assert action.materialization_version == selected_version
        assert action.payload["message_text"] == "签到"
        validate_emergency_selection(check, action, SendMessagePayload.model_validate(action.payload))
        assert check.scalar(select(func.count(AiGroupEmergencySelection.id))) == 1
        assert check.get(GenerationJob, "job").state == "failed"
    assert drain_emergency_content(factory, 20) == 0


def test_legacy_projection_alignment_serializes_with_formal_registration(factory):
    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError
    from app.models import FulfillmentObligationProjection
    from app.services.task_center.fulfillment_remote_facts import ensure_action_obligation

    with factory() as session:
        _seed(session)
        action = session.get(Action, "action")
        assert select_emergency_content(session, session.get(Task, "task"), action)
        projection = session.scalar(select(FulfillmentObligationProjection))
        projection.materialization_version = action.materialization_version - 1
        session.commit()
    with factory() as repair:
        action = repair.get(Action, "action")
        assert ensure_action_obligation(repair, action)
        with factory() as contender:
            contender.execute(text("SET LOCAL lock_timeout = '100ms'"))
            with pytest.raises(OperationalError, match="lock timeout"):
                ensure_action_obligation(contender, contender.get(Action, "action"))
            contender.rollback()
        repair.commit()
    with factory() as check:
        action = check.get(Action, "action")
        projection = check.scalar(select(FulfillmentObligationProjection))
        before_version = projection.version
        assert ensure_action_obligation(check, action)
        assert action.materialization_version == projection.materialization_version
        assert projection.version == before_version
        assert projection.active_action_id == action.id
        validate_emergency_selection(check, action, SendMessagePayload.model_validate(action.payload))

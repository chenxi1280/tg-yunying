import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import AiGroupEmergencySelection, AiGroupMessageMemory, ExecutionAttempt, FulfillmentObligationProjection, Task
from app.services._common import _now
from app.services.task_center.ai_group_emergency import select_emergency_content, validate_emergency_selection
from app.services.task_center.ai_group_emergency_pending import persist_emergency_batch
from app.services.task_center.ai_group_emergency_projection_repair import (
    REPAIR_AUDIT_KEY, align_existing_emergency_projection,
)
from app.services.task_center.fulfillment_remote_facts import ensure_action_obligation
from app.services.task_center.payloads import SendMessagePayload
from tests.ai_group_emergency_support import seed_emergency_batch


pytestmark = pytest.mark.no_postgres


@pytest.fixture
def selected():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, autoflush=False) as session:
        task, actions, coverages, request = seed_emergency_batch(session)
        assert persist_emergency_batch(session, request, reason="provider_route_exhausted")
        session.commit()
        action = actions[0]
        assert select_emergency_content(session, task, action)
        session.commit()
        projection = session.scalar(select(FulfillmentObligationProjection).where(
            FulfillmentObligationProjection.active_action_id == action.id))
        selection = session.get(AiGroupEmergencySelection, action.payload["emergency_selection_id"])
        yield session, action, projection, selection, coverages[0]
    engine.dispose()


def _legacy(selected, *, action_already_downgraded=False):
    session, action, projection, selection, _ = selected
    projection.materialization_version = selection.materialization_version - 1
    if action_already_downgraded:
        action.materialization_version = projection.materialization_version
    session.commit()


@pytest.mark.parametrize("status", ["pending", "claiming", "executing"])
@pytest.mark.parametrize("downgraded", [False, True])
def test_real_ensure_repairs_exact_legacy_revision_once(selected, status, downgraded):
    session, action, projection, selection, coverage = selected
    _legacy(selected, action_already_downgraded=downgraded)
    action.status = status
    action.result = {**action.result, "caller_change": "retained"}
    before_version = projection.version
    assert ensure_action_obligation(session, action)
    session.commit()
    assert action.status == status and action.result["caller_change"] == "retained"
    assert action.materialization_version == projection.materialization_version == selection.materialization_version
    assert projection.version == before_version + 1
    audit = dict(action.result[REPAIR_AUDIT_KEY])
    assert audit["selection_id"] == selection.id
    assert audit["previous_materialization_version"] == selection.materialization_version - 1
    assert audit["previous_projection_version"] == before_version
    assert audit["projection_version"] == before_version + 1
    assert coverage.state == "reserved" and coverage.reserved_action_id == action.id
    assert ensure_action_obligation(session, action)
    assert projection.version == before_version + 1 and action.result[REPAIR_AUDIT_KEY] == audit
    validate_emergency_selection(session, action, SendMessagePayload.model_validate(action.payload))


@pytest.mark.parametrize("status", ["failed", "unknown_after_send", "skipped", "success", "retryable_failed"])
def test_terminal_or_unknown_legacy_actions_are_never_repaired(selected, status):
    session, action, projection, selection, _ = selected
    _legacy(selected)
    action.status = status
    with pytest.raises(ValueError, match="not_exact_uncalled_revision"):
        ensure_action_obligation(session, action)
    assert action.status == status
    assert projection.materialization_version == selection.materialization_version - 1
    assert REPAIR_AUDIT_KEY not in action.result


@pytest.mark.parametrize("mutation", ["owner", "scope", "projection_ahead", "action_ahead", "content", "memory", "called"])
def test_nonexact_or_called_revision_rejects_without_advancing(selected, mutation):
    session, action, projection, selection, _ = selected
    _legacy(selected)
    if mutation == "owner":
        projection.active_action_id = "different-action"
    elif mutation == "scope":
        projection.task_lifecycle_epoch += 1
    elif mutation == "projection_ahead":
        projection.materialization_version = selection.materialization_version + 1
    elif mutation == "action_ahead":
        action.materialization_version = selection.materialization_version + 1
    elif mutation == "content":
        action.payload = {**action.payload, "message_text": "changed"}
    elif mutation == "memory":
        session.get(AiGroupMessageMemory, action.payload["ai_message_memory_id"]).raw_text = "changed"
    else:
        session.add(ExecutionAttempt(action_id=action.id, tenant_id=1, account_id=action.account_id,
            status="unknown_after_send", gateway_call_started_at=_now()))
    session.commit()
    expected_version = projection.materialization_version
    with pytest.raises(ValueError, match="emergency_"):
        ensure_action_obligation(session, action)
    assert projection.materialization_version == expected_version
    assert REPAIR_AUDIT_KEY not in action.result


def test_aligned_historical_readback_does_not_require_no_gateway_or_open_state(selected):
    session, action, projection, selection, _ = selected
    action.status = "success"
    projection.state = "fulfilled"
    session.add(ExecutionAttempt(action_id=action.id, tenant_id=1, account_id=action.account_id,
        status="success", gateway_call_started_at=_now()))
    session.commit()
    before_version = projection.version
    align_existing_emergency_projection(session, action)
    assert action.status == "success" and projection.state == "fulfilled"
    assert projection.version == before_version
    assert action.materialization_version == selection.materialization_version
    assert REPAIR_AUDIT_KEY not in action.result


def test_aligned_projection_never_silently_overwrites_bad_action_version(selected):
    session, action, projection, selection, _ = selected
    action.materialization_version = selection.materialization_version - 1
    session.commit()
    with pytest.raises(ValueError, match="action_version_invalid"):
        ensure_action_obligation(session, action)
    assert action.materialization_version == selection.materialization_version - 1
    assert projection.materialization_version == selection.materialization_version


@pytest.mark.parametrize("entry", ["expire_legacy_anchor_rewritten_actions", "expire_incomplete_daily_contract_actions"])
def test_writable_maintenance_repairs_legacy_selection_and_keeps_coverage(selected, entry):
    from app.services.task_center import legacy_anchor_rewrite as maintenance

    session, action, projection, selection, coverage = selected
    _legacy(selected)
    action.payload = {**action.payload, "voice_profile_contract_version": ""}
    session.commit()
    getattr(maintenance, entry)(session, session.get(Task, action.task_id))
    assert action.status == "pending"
    assert projection.materialization_version == action.materialization_version == selection.materialization_version
    assert action.result[REPAIR_AUDIT_KEY]["selection_id"] == selection.id
    assert coverage.state == "reserved" and coverage.reserved_action_id == action.id
    assert session.get(AiGroupMessageMemory, action.payload["ai_message_memory_id"]).status == "reserved"


def test_send_precondition_does_not_repair_projection(selected):
    from app.services.task_center import legacy_anchor_rewrite as maintenance

    session, action, projection, selection, _ = selected
    _legacy(selected)
    assert maintenance.reject_legacy_anchor_rewrite_before_send(session, action)
    assert projection.materialization_version == selection.materialization_version - 1
    assert REPAIR_AUDIT_KEY not in action.result

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import AiGroupMessageMemory, ExecutionAttempt
from app.services._common import _now
from app.services.task_center import legacy_anchor_rewrite as maintenance
from app.services.task_center.ai_group_emergency import select_emergency_content
from app.services.task_center.ai_group_emergency_pending import persist_emergency_batch
from tests.ai_group_emergency_support import seed_emergency_batch


pytestmark = pytest.mark.no_postgres


@pytest.fixture
def selected():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, autoflush=False) as session:
        task, actions, coverages, request = seed_emergency_batch(session)
        persist_emergency_batch(session, request, reason="provider_route_exhausted")
        session.commit()
        for action in actions:
            assert select_emergency_content(session, task, action)
        session.commit()
        yield session, task, actions, coverages
    engine.dispose()


def test_valid_emergency_survives_both_legacy_maintenance_paths_and_send_precondition(selected):
    session, task, actions, coverages = selected
    for action in actions:
        action.payload = {**action.payload, "voice_profile_contract_version": ""}
    assert maintenance.expire_legacy_anchor_rewritten_actions(session, task) == 0
    assert maintenance.expire_incomplete_daily_contract_actions(session, task) == 0
    for action, coverage in zip(actions, coverages):
        assert not maintenance.reject_legacy_anchor_rewrite_before_send(session, action)
        assert action.status == "pending"
        assert coverage.state == "reserved" and coverage.reserved_action_id == action.id
        assert session.get(AiGroupMessageMemory, action.payload["ai_message_memory_id"]).status == "reserved"


@pytest.mark.parametrize("corruption", ["flag", "content", "memory", "disabled"])
def test_invalid_selection_is_explicitly_rejected_instead_of_trusting_payload_flag(selected, corruption):
    session, task, actions, _ = selected
    action = actions[0]
    if corruption == "disabled":
        task.type_config = {**task.type_config, "emergency_fallback_enabled": False}
    else:
        key, value = {"flag": ("emergency_selection_id", ""), "content": ("message_text", "different"),
                      "memory": ("ai_message_memory_id", "missing")}[corruption]
        action.payload = {**action.payload, key: value}
    assert maintenance.reject_legacy_anchor_rewrite_before_send(session, action)
    assert action.status == "skipped"
    assert action.result["error_code"].startswith("emergency_")


def test_invalid_selection_with_prior_telegram_evidence_preserves_original_coverage(selected):
    session, task, actions, coverages = selected
    action = actions[0]
    session.add(ExecutionAttempt(action_id=action.id, tenant_id=1, account_id=action.account_id,
                                status="unknown_after_send", gateway_call_started_at=_now()))
    session.flush()
    task.type_config = {**task.type_config, "emergency_fallback_enabled": False}
    assert maintenance.reject_legacy_anchor_rewrite_before_send(session, action)
    assert action.status == "pending"
    assert coverages[0].state == "reserved" and coverages[0].reserved_action_id == action.id
    assert session.get(AiGroupMessageMemory, action.payload["ai_message_memory_id"]).status == "reserved"


def test_valid_emergency_prefix_does_not_starve_invalid_later_selection(selected, monkeypatch):
    session, task, actions, _ = selected
    earlier, later = sorted(actions, key=lambda row: row.id)
    later.payload = {**later.payload, "ai_message_memory_id": "missing"}
    monkeypatch.setattr(maintenance, "ACTION_MAINTENANCE_BATCH_LIMIT", 1)
    assert maintenance.expire_incomplete_daily_contract_actions(session, task) == 1
    assert earlier.status == "pending"
    assert later.status == "skipped"
    assert later.result["error_code"] == "emergency_message_memory_invalid"

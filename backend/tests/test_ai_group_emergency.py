from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Action, AiGroupEmergencySelection, ExecutionAttempt, GenerationJob, ProviderHttpExchange, ProviderHttpExchangeJob
from app.services._common import _now
from app.services.task_center.ai_generation_commit import load_generation_batch
from app.services.task_center.ai_generation_dispatch import ensure_send_message_content
from app.services.task_center.ai_generation_state import GenerationAttemptStale
from app.services.task_center.ai_group_emergency import select_emergency_content, validate_emergency_selection
from app.services.task_center.ai_group_emergency_contract import UNICODE_POOL
from app.services.task_center.ai_group_emergency_pending import persist_emergency_batch
from app.services.task_center.ai_group_emergency_worker import drain_emergency_content
from app.services.task_center.ai_group_content_projection import intent_remote_state
from app.services.task_center.payloads import SendMessagePayload
from tests.ai_group_emergency_support import seed_emergency_batch
from tests.ai_generation_phase_test_support import generation_dependencies


pytestmark = pytest.mark.no_postgres


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, autoflush=False) as current:
        yield current
    engine.dispose()


def _pending(session):
    task, actions, coverages, request = seed_emergency_batch(session)
    assert persist_emergency_batch(session, request, reason="provider_route_exhausted")
    session.commit()
    return task, actions, coverages, request


def test_exhausted_explicit_model_two_stage_keeps_original_owners_and_readies_two_check_ins(session):
    task, actions, coverages, request = _pending(session)
    factory = lambda: Session(session.get_bind(), autoflush=False)
    assert drain_emergency_content(factory, 20) == 2
    assert drain_emergency_content(factory, 20) == 0
    session.expire_all()
    for action, coverage in zip(actions, coverages):
        assert action.status == "pending" and action.payload["message_text"] == "签到"
        assert coverage.state == "reserved" and coverage.reserved_action_id == action.id
        assert session.get(GenerationJob, action.payload["generation_job_id"]).state == "failed"
        payload = SendMessagePayload.model_validate(action.payload)
        validate_emergency_selection(session, action, payload)
        assert intent_remote_state(None, action, "typed-observed") == "independent_confirmed"
        assert ensure_send_message_content(session, action, SimpleNamespace(id=action.account_id),
            payload=payload, dependencies=generation_dependencies(), allow_provider_call=False).message_text == "签到"
    assert session.scalar(select(func.count(AiGroupEmergencySelection.id))) == 2
    with pytest.raises(GenerationAttemptStale):
        load_generation_batch(session, request)


@pytest.mark.parametrize("mutation", ["content", "account", "selection", "disabled", "material"])
def test_gateway_revalidates_selection_and_original_identity(session, mutation):
    task, actions, _, _ = _pending(session)
    action = actions[0]
    assert select_emergency_content(session, task, action)
    original = SendMessagePayload.model_validate(action.payload)
    if mutation == "account":
        action.account_id = 12
    elif mutation == "disabled":
        task.type_config = {**task.type_config, "emergency_fallback_enabled": False}
    else:
        key, value = {"content": ("message_text", "different"), "selection": ("emergency_selection_id", "old"),
                      "material": ("material_intent", "image")}[mutation]
        original = original.model_copy(update={key: value})
    with pytest.raises(ValueError, match="emergency_"):
        validate_emergency_selection(session, action, original)


@pytest.mark.parametrize("status", ["unknown_after_send", "success", "before_call"])
def test_any_gateway_start_blocks_content_replacement(session, status):
    task, actions, _, _ = _pending(session)
    session.add(ExecutionAttempt(action_id=actions[0].id, tenant_id=1, account_id=11,
        status=status, gateway_call_started_at=_now()))
    session.flush()
    assert not select_emergency_content(session, task, actions[0])
    assert not actions[0].payload.get("emergency_selection_id")


def test_reply_fallback_keeps_real_parent_and_never_becomes_direct_check_in(session):
    task, actions, _, _ = _pending(session)
    action = actions[0]
    action.payload = {**action.payload, "reply_to_message_id": 9001, "relation_kind": "reply", "chat_mode": "reply"}
    assert select_emergency_content(session, task, action)
    assert action.payload["message_text"] in UNICODE_POOL
    assert action.payload["reply_to_message_id"] == 9001
    assert action.payload["relation_kind"] == "reply"


@pytest.mark.parametrize("locally_ended", [False, True])
def test_provider_unknown_requires_durable_local_end_and_never_rewrites_unknown(session, locally_ended):
    task, actions, _, _ = _pending(session)
    action = actions[0]
    job = session.get(GenerationJob, action.payload["generation_job_id"])
    job.state = "unknown"
    action.payload = {**action.payload, "ai_generation_status": "provider_result_unknown"}
    exchange = ProviderHttpExchange(id="unknown-exchange", chain_id="unknown-chain", tenant_id=1,
        task_id=task.id, task_lifecycle_epoch=1, provider_id=1, logical_request_id="old-request",
        model_name="old-model", purpose="群活跃续聊", request_hash="hash", outcome="unknown",
        local_termination_confirmed=locally_ended, started_at=_now())
    session.add(exchange)
    session.add(ProviderHttpExchangeJob(exchange_id=exchange.id, generation_job_id=job.id, execution_path_hash="path"))
    session.flush()
    assert select_emergency_content(session, task, action) is locally_ended
    assert job.state == "unknown" and exchange.outcome == "unknown"


@pytest.mark.parametrize("invalid", ["task_epoch", "coverage", "terminal_slot", "ready", "claim"])
def test_stale_or_already_owned_work_cannot_select(session, invalid):
    task, actions, coverages, _ = _pending(session)
    action = actions[0]
    if invalid == "task_epoch":
        task.task_lifecycle_epoch += 1
    elif invalid == "coverage":
        coverages[0].reserved_action_id = "another-action"
    elif invalid == "terminal_slot":
        from app.models import TaskGroupDailyMessageSlot
        session.get(TaskGroupDailyMessageSlot, action.primary_quantity_slot_id).state = "terminal"
    elif invalid == "claim":
        action.claim_owner = "dispatcher"
    else:
        action.payload = {**action.payload, "message_text": "normal ready", "ai_generation_status": "ready"}
    assert not select_emergency_content(session, task, action)


def test_stale_payload_cannot_send_after_current_reply_identity_changes(session):
    task, actions, _, _ = _pending(session)
    action = actions[0]
    assert select_emergency_content(session, task, action)
    old = SendMessagePayload.model_validate(action.payload)
    action.payload = {**action.payload, "reply_to_message_id": 123456}
    with pytest.raises(ValueError, match="emergency_selection_publication_stale"):
        validate_emergency_selection(session, action, old)


def test_previous_action_for_same_quantity_with_gateway_start_cannot_be_replaced(session):
    task, actions, _, _ = _pending(session)
    sibling = Action(id="old-quantity-action", tenant_id=1, task_id=task.id, task_type="group_ai_chat",
        action_type="send_message", account_id=11, primary_quantity_slot_id=actions[0].primary_quantity_slot_id,
        status="unknown_after_send", scheduled_at=_now())
    session.add(sibling)
    session.add(ExecutionAttempt(action_id=sibling.id, tenant_id=1, account_id=11,
        status="unknown_after_send", gateway_call_started_at=_now()))
    session.flush()
    assert not select_emergency_content(session, task, actions[0])


def test_missing_topic_before_provider_enters_emergency_without_losing_quantity(session):
    from app.services.task_center.ai_generation_worker import drain_ai_generation
    from app.services.task_center.ai_generator import AiGenerationUnavailable
    from app.models import TgAccount, TgGroup

    _, actions, coverages, _ = seed_emergency_batch(session)
    session.get(TgGroup, 7).listener_last_error = "no usable reader"
    session.commit()
    action = actions[0]
    with pytest.raises(AiGenerationUnavailable, match="topic_only_topic_missing"):
        ensure_send_message_content(session, action, session.get(TgAccount, 11),
            payload=SendMessagePayload.model_validate(action.payload), dependencies=generation_dependencies())
    assert action.payload["ai_generation_status"] == "emergency_pending"
    assert coverages[0].reserved_action_id == action.id and coverages[0].state == "reserved"
    assert drain_ai_generation(lambda: Session(session.get_bind(), autoflush=False), limit=1,
                               dependencies=generation_dependencies()) == 1
    session.refresh(action)
    assert action.payload["message_text"] == "签到"


def test_quality_exhaustion_preserves_owners_through_parallel_worker_settlement(session):
    from app.services.task_center.ai_generation_persistence import _persist_generation_rejection
    from app.services.task_center.ai_generation_pipeline import SlotGenerationResult
    from app.services.task_center.ai_generation_parallel_settlement import settle_parallel_outcome
    from app.services.task_center.ai_generation_worker_types import GenerationOutcome
    from app.services.task_center.ai_generator import AiGenerationUnavailable

    _, actions, coverages, request = seed_emergency_batch(session)
    action = actions[0]
    _persist_generation_rejection(session, request, action=action,
        result=SlotGenerationResult("", "quality_wait", "normal quality budget exhausted"))
    session.commit()
    claim = SimpleNamespace(action_id=action.id, owner=request.claim_owner, token=request.claim_token,
                            job_id=action.payload["generation_job_id"])
    assert settle_parallel_outcome(lambda: Session(session.get_bind(), autoflush=False), claim,
        GenerationOutcome(failure=AiGenerationUnavailable("emergency_pending"))) == 1
    assert action.status == "pending" and action.payload["ai_generation_status"] == "emergency_pending"
    assert coverages[0].state == "reserved" and coverages[0].reserved_action_id == action.id

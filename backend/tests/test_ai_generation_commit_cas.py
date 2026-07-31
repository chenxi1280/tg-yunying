from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Action, Task, Tenant
from app.services._common import _now
from app.services.task_center import ai_generation_dispatch
from app.services.task_center.ai_generation_commit import load_generation_batch
from app.services.task_center.ai_generation_state import (
    apply_generated_content_metadata,
    cached_generation_result,
    generation_result_cache,
)
from app.services.task_center.ai_generator import GeneratedContent
from app.services.task_center.payloads import SendMessagePayload


pytestmark = pytest.mark.no_postgres


def test_phase_c_rejects_stale_claim_token_without_mutating_action() -> None:
    with _generation_session() as session:
        action = _generation_action()
        session.add(action)
        session.commit()
        request = _request(claim_token="old-token")

        with pytest.raises(ai_generation_dispatch.GenerationAttemptStale):
            load_generation_batch(session, request)

        session.refresh(action)
        assert action.status == "executing"
        assert action.claim_token == "new-token"
        assert action.payload["ai_generation_attempt_id"] == "attempt-current"


def test_phase_c_rejects_cross_tenant_action_id() -> None:
    with _generation_session(tenant_id=2) as session:
        action = _generation_action(tenant_id=2, task_id="task-2")
        session.add(action)
        session.commit()

        with pytest.raises(ai_generation_dispatch.GenerationAttemptStale):
            load_generation_batch(session, _request(tenant_id=1))

        assert session.get(Action, action.id).tenant_id == 2


def test_phase_c_loads_only_matching_lease_and_attempt() -> None:
    with _generation_session() as session:
        action = _generation_action()
        session.add(action)
        session.commit()

        [(loaded, payload)] = load_generation_batch(session, _request())

        assert loaded.id == action.id
        assert payload.ai_generation_attempt_id == "attempt-current"


def test_phase_c_copies_provider_audit_metadata() -> None:
    content = GeneratedContent(
        "真实文案",
        requested_model="MiniMax-M3",
        actual_model="MiniMax-M2.5",
        generation_source="static_safe_fallback",
        quality_fallback="emoji_react",
        fallback_stage="fallback_m25",
        fallback_reason="primary_rejected",
        provider_duration_ms=321,
        generation_attempts=[{"stage": "primary_m3", "outcome": "rejected"}],
    )

    data = apply_generated_content_metadata({}, content)

    assert data["requested_model"] == "MiniMax-M3"
    assert data["actual_model"] == "MiniMax-M2.5"
    assert data["generation_source"] == "static_safe_fallback"
    assert data["quality_fallback"] == "emoji_react"
    assert data["fallback_stage"] == "fallback_m25"
    assert data["fallback_reason"] == "primary_rejected"
    assert data["provider_duration_ms"] == 321
    assert data["generation_attempts"] == [{"stage": "primary_m3", "outcome": "rejected"}]
    payload = SendMessagePayload(
        group_id=7,
        ai_generation_status="ai_result_persist_unknown",
        ai_generation_attempt_id="attempt-current",
        content_scope_contract_version="group_content_scope_v1",
        content_scope_tenant_id=1,
        content_scope_group_id=7,
        content_scope_task_id="task-1",
    )
    cache = generation_result_cache(
        content,
        9,
        "attempt-current",
        payload=payload,
    )
    payload = payload.model_copy(update={"ai_generation_result_cache": cache})
    restored, tokens = cached_generation_result(payload)
    restored_data = apply_generated_content_metadata({}, restored)
    assert tokens == 9
    assert restored_data == data


def test_phase_c_preserves_planned_generation_source_when_provider_omits_it() -> None:
    content = GeneratedContent(
        "真实文案",
        actual_model="mimo-v2.5",
        fallback_stage="primary_default",
    )

    data = apply_generated_content_metadata(
        {"generation_source": "human_context"},
        content,
    )

    assert data["generation_source"] == "human_context"


def test_cached_generation_rejects_scope_mismatch() -> None:
    payload = SendMessagePayload(
        group_id=8,
        ai_generation_status="ai_result_persist_unknown",
        ai_generation_attempt_id="attempt-current",
        content_scope_contract_version="group_content_scope_v1",
        content_scope_tenant_id=1,
        content_scope_group_id=8,
        content_scope_task_id="task-b",
        ai_generation_result_cache={
            "content": "A群旧缓存",
            "tokens": 9,
            "attempt_id": "attempt-current",
            "content_scope_contract_version": "group_content_scope_v1",
            "content_scope_tenant_id": 1,
            "content_scope_group_id": 7,
            "content_scope_task_id": "task-a",
        },
    )

    assert cached_generation_result(payload) is None


def _generation_session(tenant_id: int = 1) -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session = Session(engine)
    session.add(Tenant(id=tenant_id, name=f"tenant-{tenant_id}"))
    session.add(Task(id=f"task-{tenant_id}", tenant_id=tenant_id, name="task", type="group_ai_chat", status="running"))
    session.commit()
    return session


def _generation_action(*, tenant_id: int = 1, task_id: str = "task-1") -> Action:
    return Action(
        id="generation-action",
        tenant_id=tenant_id,
        task_id=task_id,
        task_type="group_ai_chat",
        action_type="send_message",
        account_id=11,
        status="executing",
        claim_owner="worker-new",
        claim_token="new-token",
        scheduled_at=_now(),
        payload={
            "group_id": 7,
            "message_text": "",
            "ai_generation_status": "generating",
            "ai_generation_attempt_id": "attempt-current",
            "ai_generation_claim_owner": "worker-new",
            "ai_generation_claim_token": "new-token",
        },
    )


def _request(*, tenant_id: int = 1, claim_token: str = "new-token") -> SimpleNamespace:
    return SimpleNamespace(
        tenant_id=tenant_id,
        task_id="task-1",
        group_id=7,
        batch_ids=["generation-action"],
        claim_owner="worker-new",
        claim_token=claim_token,
        attempt_id="attempt-current",
    )

from sqlalchemy import select

from app.models import Action, AiGroupMessageMemory


def assert_persist_unknown_state(session, action, *, coverage, observed) -> None:
    session.refresh(action)
    session.refresh(coverage)
    assert action.status == "pending"
    assert action.payload["ai_generation_status"] == "ai_result_persist_unknown"
    assert action.payload["ai_generation_result_cache"]["content"] == "就按这个节奏来"
    assert coverage.state == "reserved"
    assert observed == {"provider_calls": 1, "gateway_calls": 0}
    assert list(session.scalars(select(AiGroupMessageMemory))) == []


def assert_recovery_completed(session, action, *, coverage, observed) -> None:
    assert action.status == "success", action.result.get("error_code")
    assert action.payload["ai_generation_status"] == "ready"
    assert action.payload["ai_generation_result_cache"] == {}
    assert observed == {"provider_calls": 1, "gateway_calls": 1}
    memory = session.scalar(select(AiGroupMessageMemory).where(AiGroupMessageMemory.action_id == action.id))
    assert memory.status == "success"
    assert coverage.state == "confirmed"
    assert coverage.confirmed_count == 1


def assert_cas_fence_preserved(session, action_id: str) -> None:
    action = session.get(Action, action_id)
    assert action.payload["ai_generation_claim_owner"] == "new-worker"
    assert action.payload["ai_generation_claim_token"] == "new-token"
    assert action.payload["ai_generation_attempt_id"] == "new-attempt"
    assert action.payload["ai_generation_status"] == "generating"
    assert session.scalar(select(AiGroupMessageMemory).where(
        AiGroupMessageMemory.action_id == action_id,
    )) is None

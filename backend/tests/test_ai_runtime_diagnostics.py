from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    Action,
    AiProviderAttempt,
    FulfillmentRemoteFact,
    FulfillmentShortfallFact,
    GenerationJob,
    Task,
)
from app.services.task_center.ai_runtime_diagnostics import ai_runtime_diagnostics


pytestmark = pytest.mark.no_postgres


def test_runtime_diagnostics_exposes_status_funnel_revisions_and_full_tokens() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        task = Task(
            id="task-runtime",
            tenant_id=1,
            name="runtime",
            type="group_ai_chat",
            status="running",
        )
        job = GenerationJob(
            id="job-runtime",
            tenant_id=1,
            task_id=task.id,
            obligation_type="quantity_slot",
            obligation_id="slot-runtime",
            generation_sequence=1,
            context_snapshot_version=2,
            context_route="general",
            prompt_contract_version="general_v3",
            voice_profile_version="voice_v3",
            provider_route_set_revision=4,
            generation_stage="quality_wait",
            candidate_hash="c" * 64,
            state="pending",
        )
        action = Action(
            id="action-runtime",
            tenant_id=1,
            task_id=task.id,
            task_type="group_ai_chat",
            action_type="send_message",
            scheduled_at=datetime(2026, 8, 25, 12, 0),
            status="pending",
            payload={
                "ai_generation_status": "ready",
                "dialogue_chain_state": "parent_remote_fact_bound",
            },
        )
        session.add_all((task, job, action, _attempt(), _shortfall(), _remote_fact()))
        session.flush()

        result = ai_runtime_diagnostics(session, task)

        assert result["generation_stage_counts"] == {"quality_wait": 1}
        assert result["shortfall_counts"] == {"quality": 1}
        assert result["dialogue_chain_state_counts"] == {"parent_remote_fact_bound": 1}
        assert result["token_ledger"] == {
            "attempt_count": 1,
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_tokens": 40,
            "cost_amount": 0.12,
            "outcome_counts": {"success": 1},
            "purpose_counts": {"content_generator:general": 1},
        }
        assert result["conversion_funnel"] == {
            "generation_jobs": 1,
            "accepted_candidates": 1,
            "provider_attempts": 1,
            "ready_actions": 1,
            "telegram_remote_success": 1,
        }
        assert result["active_revisions"] == {
            "route": ["general"],
            "prompt": ["general_v3"],
            "voice": ["voice_v3"],
            "provider": [4],
        }


def _attempt() -> AiProviderAttempt:
    return AiProviderAttempt(
        generation_job_id="job-runtime",
        purpose="content_generator:general",
        route_set_id="route-runtime",
        route_set_revision=4,
        provider_id=1,
        model_name="model-a",
        priority=1,
        attempt_index=1,
        request_hash="r" * 64,
        outcome="success",
        prompt_tokens=100,
        completion_tokens=20,
        cached_tokens=40,
        cost_amount=0.12,
    )


def _shortfall() -> FulfillmentShortfallFact:
    return FulfillmentShortfallFact(
        tenant_id=1,
        task_id="task-runtime",
        owner_type="quantity_slot",
        owner_id="slot-runtime",
        period_key="period-runtime",
        kind="quality",
        reason_code="quality_wait_deadline",
        requested_quantity=1,
        settled_quantity=0,
        evidence_hash="e" * 64,
    )


def _remote_fact() -> FulfillmentRemoteFact:
    return FulfillmentRemoteFact(
        tenant_id=1,
        task_type="group_ai_chat",
        task_id="task-runtime",
        obligation_type="quantity_slot",
        obligation_id="slot-runtime",
        action_id="action-runtime",
        attempt_id="attempt-runtime",
        mutation_kind="send_message",
        remote_mutation_key_hash="a" * 64,
        gateway_request_hash="b" * 64,
        fact_kind="remote_message_observed",
        fact_identity_hash="f" * 64,
        outcome={"remote_message_id": "999"},
    )

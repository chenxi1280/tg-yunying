"""Verify atomic expiry and rollback with a real local PostgreSQL database."""
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.models import (Action, AccountPacingReservation, ChannelMessage,
                        CommentFulfillmentObligation, GenerationJob, OperationTarget, Task, Tenant, TgAccount)
from app.services._common import _now
from app.services.task_center.ai_generation_claim_lifecycle import mark_generation_claim
from app.services.task_center import comment_generation_dispatch as dispatch
from app.services.task_center import comment_generation_worker as worker
from tests.postgres_pacing_e4_fixture import factory
from tests.test_comment_preparation_expiry import _expired_preparation

pytestmark = [pytest.mark.isolated_postgres, pytest.mark.allow_missing_rule_binding]


@pytest.mark.parametrize("obligation_state", ["pending", "unknown"])
def test_postgres_expiry_commits_or_rolls_back_as_one_unit(factory, monkeypatch, obligation_state):
    monkeypatch.setattr(dispatch, "_generation_config", _expired_preparation)
    with factory() as seed:
        action, claim = _prepare_expired(seed)
        obligation = seed.get(CommentFulfillmentObligation, "comment-obligation-1")
        obligation.status = obligation_state
        seed.commit()
        action_id = action.id
    if obligation_state == "unknown":
        with pytest.raises(RuntimeError, match="comment_obligation_not_pending"):
            worker._process_comment_generation(factory, claim,
                dependencies=worker.PRODUCTION_COMMENT_GENERATION_DEPENDENCIES)
    else:
        worker._process_comment_generation(factory, claim,
            dependencies=worker.PRODUCTION_COMMENT_GENERATION_DEPENDENCIES)
    with factory() as readback:
        action = readback.get(Action, action_id)
        obligation = readback.get(CommentFulfillmentObligation, "comment-obligation-1")
        reservation = readback.scalar(select(AccountPacingReservation))
        job = readback.scalar(select(GenerationJob))
        if obligation_state == "pending":
            assert (action.status, obligation.status, reservation.state, job.state) == (
                "failed", "terminal_shortfall", "missed", "failed")
        else:
            assert (action.status, obligation.status, reservation.state) == ("pending", "unknown", "bound")
            assert job is None


def _prepare_expired(session):
    task_id = "neutral-expiry-task"
    deadline = _now() - timedelta(seconds=1)
    rows = (
        Tenant(id=1, name="neutral expiry"),
        TgAccount(id=101, tenant_id=1, display_name="neutral account", phone_masked="***"),
        OperationTarget(id=31, tenant_id=1, target_type="channel", tg_peer_id="-10031", title="neutral channel"),
        ChannelMessage(id=41, tenant_id=1, channel_target_id=31, message_id=9001),
        Task(id=task_id, tenant_id=1, name="neutral expiry", type="channel_comment", status="running"),
    )
    for row in rows:
        session.add(row)
        session.flush()
    action = Action(id="neutral-action", tenant_id=1, task_id=task_id, task_type="channel_comment",
        action_type="post_comment", account_id=101, payload={"channel_id": "-10031", "message_id": 9001,
        "channel_message_id": 41, "comment_fulfillment_obligation_id": "comment-obligation-1"})
    claim = worker.CommentGenerationClaim(action.id, "neutral-owner", "neutral-token")
    mark_generation_claim(action, claim.owner, claim.token)
    session.add(action)
    session.flush()
    session.add(CommentFulfillmentObligation(id="comment-obligation-1", tenant_id=1, task_id=task_id,
        channel_message_id=41, comment_plan_revision=1, target_ordinal=1, current_action_id=action.id,
        status="pending"))
    session.add(AccountPacingReservation(tenant_id=1, task_id=task_id, account_id=101,
        pacing_slot_key="neutral-expired-comment", policy_version="soft_pacing_v1", due_at=deadline,
        release_not_before_at=deadline, effective_claim_at=deadline, source_deadline_at=deadline,
        action_id=action.id, state="bound"))
    session.commit()
    return action, claim

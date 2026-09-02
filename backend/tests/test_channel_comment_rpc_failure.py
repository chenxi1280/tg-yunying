from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.integrations.telegram.contracts import SendResult
from app.models import Action, ChannelDiscussionThreadBinding, DiscussionMembershipFact
from app.services.task_center.channel_comment_discussion_contracts import current_membership_fact
from app.services.task_center.channel_comment_discussion_freshness import thread_binding_fresh
from app.services.task_center.channel_comment_rpc_failure import project_comment_pre_mutation_failure
from app.services.task_center.channel_payloads import PostCommentPayload
from app.services.task_center.executors import channel_comment
from channel_comment_planner_test_support import (
    STABLE_PLANNER_NOW,
    fixed_profile,
    forbid_planner_external_boundaries,
    planner_session,
    seed_comment_task,
)
from test_channel_comment_plan_contract import _enable_grounding_plan


pytestmark = pytest.mark.no_postgres


def _planned_comment(session, monkeypatch):
    forbid_planner_external_boundaries(monkeypatch)
    fixed_profile(monkeypatch)
    task = seed_comment_task(session, mode="comment", target_count=3)
    _enable_grounding_plan(session, task)
    channel_comment.build_plan(session, task)
    action = session.scalar(select(Action).where(Action.action_type == "post_comment"))
    return action, PostCommentPayload.model_validate(action.payload)


def test_membership_reject_writes_account_scoped_negative_fact(monkeypatch) -> None:
    with planner_session() as session:
        action, payload = _planned_comment(session, monkeypatch)
        projected = project_comment_pre_mutation_failure(
            session, action, payload=payload,
            result=SendResult(
                False, failure_type="discussion_send_forbidden",
                remote_mutation_started=False,
            ),
            attempt_id="attempt-1", observed_at=STABLE_PLANNER_NOW,
        )
        fact = current_membership_fact(
            session, tenant_id=action.tenant_id, account_id=action.account_id,
            discussion_peer_id=payload.discussion_peer_id,
            group_binding_id=payload.discussion_group_binding_id,
        )

        assert fact.membership_status == "restricted"
        assert fact.can_send is False
        assert projected["discussion_membership_remote_fact"]["fact_id"] == fact.id
        assert session.scalar(select(DiscussionMembershipFact).where(
            DiscussionMembershipFact.account_id != action.account_id,
            DiscussionMembershipFact.membership_status == "restricted",
        )) is None


def test_source_identity_reject_blocks_thread_until_authoritative_reprobe(monkeypatch) -> None:
    with planner_session() as session:
        action, payload = _planned_comment(session, monkeypatch)
        thread = session.get(ChannelDiscussionThreadBinding, payload.discussion_thread_binding_id)
        assert thread_binding_fresh(session, thread, STABLE_PLANNER_NOW)

        projected = project_comment_pre_mutation_failure(
            session, action, payload=payload,
            result=SendResult(
                False, failure_type="source_comment_identity_reprobe_required",
                remote_mutation_started=False,
            ),
            attempt_id="attempt-2",
            observed_at=STABLE_PLANNER_NOW + timedelta(minutes=1),
        )

        assert projected == {"source_comment_reprobe_required": True}
        assert not thread_binding_fresh(
            session, thread, STABLE_PLANNER_NOW + timedelta(minutes=1),
        )

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.models import (
    Action,
    ChannelCommentPlanContract,
    ChannelCommentPlanLifecycleEvent,
    ChannelDiscussionGroupBinding,
    ChannelDiscussionGroupProbeEvent,
    ChannelCommentListenerErrorEvent,
    ChannelMessageSourceRevision,
    CommentFulfillmentObligation,
    OperationTarget,
    TaskSourceSubscription,
)
from app.services.task_center.channel_comment_discussion_contracts import (
    AUTHORITATIVE_GROUP_STAGE,
    GroupProbeObservation,
    record_group_probe,
)
from app.services.task_center.channel_comment_discussion_freshness import group_binding_fresh
from app.services.task_center.channel_comment_discussion_read_model import (
    channel_comment_discussion_read_model,
)
from app.services.task_center.channel_listener_snapshot_persistence import _freeze_group_identity
from app.services.task_center.channel_comment_listener_errors import (
    clear_owned_listener_errors,
    record_listener_error,
)
from app.services.task_center.executors import channel_comment
from channel_comment_planner_test_support import (
    STABLE_PLANNER_NOW,
    fixed_profile,
    forbid_planner_external_boundaries,
    planner_session,
    seed_comment_task,
)
from test_channel_comment_plan_contract import _enable_grounding_plan
from test_channel_comment_discussion_admission import _enable_auto_join, _mark_accounts_not_joined


pytestmark = pytest.mark.no_postgres


def _changed_group_observation(task_id: str) -> GroupProbeObservation:
    return GroupProbeObservation(
        tenant_id=1,
        channel_target_id=31,
        target_reference_revision=1,
        channel_peer_id="-10031",
        discussion_target_id=33,
        discussion_peer_id="-10033",
        probe_request_id=f"changed-{task_id}",
        probe_status="success",
        probe_stage=AUTHORITATIVE_GROUP_STAGE,
        observed_at=STABLE_PLANNER_NOW + timedelta(minutes=5),
        fresh_until_at=STABLE_PLANNER_NOW + timedelta(days=1),
    )


def test_same_binding_probe_refreshes_event_without_mutating_binding() -> None:
    with planner_session() as session:
        task = seed_comment_task(session, mode="comment", target_count=3)
        _enable_grounding_plan(session, task)
        original = session.scalar(select(ChannelDiscussionGroupBinding))
        original_observed_at = original.observed_at
        refreshed_at = STABLE_PLANNER_NOW + timedelta(minutes=10)

        refreshed = record_group_probe(session, GroupProbeObservation(
            tenant_id=1,
            channel_target_id=31,
            target_reference_revision=2,
            channel_peer_id="-10031",
            discussion_target_id=32,
            discussion_peer_id="-10032",
            probe_request_id="same-identity-refresh",
            probe_status="success",
            probe_stage=AUTHORITATIVE_GROUP_STAGE,
            observed_at=refreshed_at,
            fresh_until_at=refreshed_at + timedelta(hours=1),
        ))
        session.flush()

        assert refreshed.id == original.id
        assert refreshed.observed_at == original_observed_at
        assert session.scalar(select(func.count(ChannelDiscussionGroupProbeEvent.id))) == 2
        assert group_binding_fresh(session, original, refreshed_at + timedelta(minutes=30))


def test_freeze_group_identity_never_mutates_existing_blank_source_revision() -> None:
    source = type("Source", (), {
        "discussion_group_binding_id": None,
        "discussion_group_binding_revision": None,
        "discussion_group_identity_hash": "",
    })()
    binding = type("Binding", (), {
        "id": "binding-1",
        "binding_revision": 1,
        "identity_hash": "a" * 64,
    })()

    assert _freeze_group_identity(source, binding) is False
    assert source.discussion_group_binding_id is None
    assert source.discussion_group_binding_revision is None
    assert source.discussion_group_identity_hash == ""


def test_group_change_fences_pre_gateway_plan_and_preserves_source(monkeypatch) -> None:
    forbid_planner_external_boundaries(monkeypatch)
    fixed_profile(monkeypatch)
    with planner_session() as session:
        task = seed_comment_task(session, mode="comment", target_count=3)
        _enable_grounding_plan(session, task)
        channel_comment.build_plan(session, task)
        source = session.scalar(select(ChannelMessageSourceRevision))
        frozen_binding_id = source.discussion_group_binding_id
        session.add(OperationTarget(
            id=33, tenant_id=1, target_type="group", tg_peer_id="-10033",
            title="换绑讨论组", can_send=True, auth_status="Telegram权威发现",
        ))
        session.flush()

        successor = record_group_probe(session, _changed_group_observation(task.id))
        session.flush()
        plan = session.scalar(select(ChannelCommentPlanContract))
        actions = list(session.scalars(select(Action).where(Action.action_type == "post_comment")))
        obligations = list(session.scalars(select(CommentFulfillmentObligation)))

        assert successor.binding_revision == 2
        assert source.discussion_group_binding_id == frozen_binding_id
        assert not _freeze_group_identity(source, successor)
        assert source.discussion_group_binding_id == frozen_binding_id
        assert plan.contract_state == "terminated_discussion_changed"
        assert all(action.status == "cancelled" for action in actions)
        assert all(item.status == "terminated" for item in obligations)
        assert session.scalar(select(func.count(ChannelCommentPlanLifecycleEvent.id))) == 1


def test_discussion_read_model_separates_account_layers() -> None:
    with planner_session() as session:
        task = seed_comment_task(session, mode="comment", target_count=3)
        _enable_grounding_plan(session, task)

        ready = channel_comment_discussion_read_model(
            session, task, now_value=STABLE_PLANNER_NOW,
        )
        binding_id = ready["binding"]["id"]
        _mark_accounts_not_joined(session, binding_id)
        _enable_auto_join(task)
        task.type_config = {**task.type_config, "discussion_join_budget": 2}
        session.commit()
        admission = channel_comment_discussion_read_model(
            session, task, now_value=STABLE_PLANNER_NOW,
        )

        assert ready["selection_mode"] == "all"
        assert ready["configured_account_ids"] == []
        assert ready["raw_online_count"] == ready["base_operational_candidate_count"] == 3
        assert ready["discussion_membership_ready_count"] == 3
        assert ready["comment_contract_eligible_count"] == ready["effective_comment_ready_count"] == 3
        assert ready["binding"]["fresh"] is True
        assert ready["enrollment"]["state"] == "active"
        assert ready["listener"]["snapshot_state"] == "pending"
        assert admission["discussion_membership_ready_count"] == 0
        assert admission["discussion_admission_required_count"] == 3
        assert admission["comment_contract_eligible_count"] == 2
        assert admission["effective_comment_ready_count"] == 0


def test_listener_ready_only_clears_exact_error_owner() -> None:
    with planner_session() as session:
        task = seed_comment_task(session, mode="comment", target_count=3)
        subscriptions = [
            TaskSourceSubscription(
                tenant_id=1, task_id=task.id, lifecycle_epoch=task.task_lifecycle_epoch,
                source_type="channel", source_peer_hash=f"source-{revision}",
                target_reference_revision=revision, listener_revision=revision,
            )
            for revision in (1, 2)
        ]
        session.add_all(subscriptions)
        session.flush()
        first = record_listener_error(
            session, task, subscriptions[0], error_code="channel_source_snapshot_pending",
            detail="pending", observed_at=STABLE_PLANNER_NOW,
        )
        second = record_listener_error(
            session, task, subscriptions[1], error_code="channel_source_snapshot_unavailable",
            detail="unavailable", observed_at=STABLE_PLANNER_NOW,
        )

        assert clear_owned_listener_errors(
            session, task, subscriptions[0], cleared_at=STABLE_PLANNER_NOW,
        ) == 1
        assert first.error_state == "cleared"
        assert second.error_state == "active"
        assert task.last_error == "channel_source_snapshot_unavailable"
        assert session.scalar(select(func.count(ChannelCommentListenerErrorEvent.id)).where(
            ChannelCommentListenerErrorEvent.error_state == "active",
        )) == 1

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import func, select

from app.models import (
    Action,
    ChannelCommentGroundingAssignment,
    ChannelCommentPlanContract,
    ChannelCommentQualityTargetRevision,
    ChannelMessage,
    ChannelMessageSourceRevision,
    CommentFulfillmentObligation,
)
from app.services.task_center.channel_comment_acceptance import channel_comment_acceptance
from app.services.task_center.channel_comment_content_revision import (
    reconcile_channel_comment_source_edit,
)
from app.services.task_center.channel_comment_quality_target import (
    build_quality_target_component,
)
from app.services.task_center.executors import channel_comment
from channel_comment_planner_test_support import (
    STABLE_PLANNER_NOW,
    fixed_profile,
    forbid_planner_external_boundaries,
    planner_session,
    seed_comment_task,
)
from test_channel_comment_plan_contract import (
    _enable_grounding_plan,
    _hold_delete_identity,
    _ordered_obligations,
)


pytestmark = pytest.mark.no_postgres


def test_initial_quality_target_is_immutable_across_task_config_edit(monkeypatch) -> None:
    forbid_planner_external_boundaries(monkeypatch)
    fixed_profile(monkeypatch)
    with planner_session() as session:
        task = seed_comment_task(session, mode="comment", target_count=3)
        _enable_grounding_plan(session, task)
        channel_comment.build_plan(session, task)
        plan = session.scalar(select(ChannelCommentPlanContract))
        target = session.get(
            ChannelCommentQualityTargetRevision,
            plan.current_quality_target_revision_id,
        )
        assignments = list(session.scalars(select(ChannelCommentGroundingAssignment)))
        actions = list(session.scalars(select(Action).where(Action.task_id == task.id)))
        task.config_revision = 2
        task.type_config = {**task.type_config, "grounding_quality_target_bps": 10000}
        session.commit()
        channel_comment.build_plan(session, task)
        acceptance = channel_comment_acceptance(session, task)

    assert plan.initial_quality_target_revision_id == target.id
    assert plan.current_quality_target_revision_id == target.id
    assert target.quality_target_revision == 1
    assert target.aggregate_grounding_required_count == 2
    assert target.aggregate_planned_fallback_count == 0
    assert all(row.quality_target_revision_id == target.id for row in assignments)
    assert {row.payload["quality_target_revision_id"] for row in actions} == {target.id}
    assert acceptance["quality_target_current_revision"] == 1
    assert acceptance["quality_target_effective_revision"] == 1
    assert acceptance["quality_target_unassigned_ordinal_count"] == 0
    assert acceptance["semantic_capacity_state"] == "sufficient"


def test_semantic_capacity_adjustment_keeps_raw_target_and_fallback() -> None:
    source = ChannelMessageSourceRevision(
        id="capacity-source", tenant_id=1, channel_message_id=41,
        source_revision=1, source_remote_message_id=9001,
        source_published_at=datetime(2030, 8, 1, 10),
        source_observed_at=STABLE_PLANNER_NOW,
        source_text_snapshot="频道事实正文", source_content_hash="a" * 64,
        observation_identity_hash="b" * 64, source_operation="observed",
    )
    component = build_quality_target_component(
        source, list(range(1, 11)), comment_grounding_revision=1,
    )

    assert component["owned_ordinal_count"] == 10
    assert component["unadjusted_grounding_target_count"] == 9
    assert component["groundable_capacity_count"] == 4
    assert component["grounding_required_count"] == 4
    assert component["planned_fallback_count"] == 6
    assert component["semantic_capacity_state"] == "capacity_adjusted"
    assert component["semantic_capacity_policy_version"]
    assert component["raw_grounding_ordinal_ids"] == [1, 2, 3, 4, 5, 6, 7, 8, 9]
    assert component["grounding_ordinal_ids"] == [1, 2, 3, 4]


def test_source_edit_revises_only_pre_gateway_quality_component(monkeypatch) -> None:
    forbid_planner_external_boundaries(monkeypatch)
    fixed_profile(monkeypatch)
    with planner_session() as session:
        task = seed_comment_task(session, mode="comment", target_count=3)
        _enable_grounding_plan(session, task)
        channel_comment.build_plan(session, task)
        plan = session.scalar(select(ChannelCommentPlanContract))
        initial_target_id = plan.current_quality_target_revision_id
        actions = sorted(
            session.scalars(select(Action).where(Action.task_id == task.id)),
            key=lambda row: int(row.payload["target_ordinal"]),
        )
        obligations = _ordered_obligations(session)
        movable = next(row for row in obligations if row.current_action_id == actions[0].id)
        held = next(row for row in obligations if row.current_action_id == actions[1].id)
        held_assignment_id = held.grounding_assignment_id
        held_payload = dict(actions[1].payload)
        _hold_delete_identity(session, actions[1], held, held_state="unknown")
        edited = _empty_edited_source(session)
        reconcile_channel_comment_source_edit(
            session, session.get(ChannelMessage, 41), edited, at=STABLE_PLANNER_NOW,
        )
        session.flush()
        target = session.get(
            ChannelCommentQualityTargetRevision,
            plan.current_quality_target_revision_id,
        )
        movable_assignment_id = movable.grounding_assignment_id
        revision_count = session.scalar(select(func.count(
            ChannelCommentQualityTargetRevision.id,
        )))

    assert target.quality_target_revision == 2
    assert target.supersedes_quality_target_revision_id == initial_target_id
    assert revision_count == 2
    assert _owned_ordinals(target.component_targets_json) == [1, 2]
    assert target.aggregate_grounding_required_count == 1
    assert target.aggregate_planned_fallback_count == 1
    assert movable_assignment_id is None
    assert movable.fallback_intent_kind == "planned"
    assert held.grounding_assignment_id == held_assignment_id
    assert actions[1].payload == held_payload
    assert actions[0].status == "cancelled"


def test_planned_fallback_is_applicable_but_never_grounded(monkeypatch) -> None:
    forbid_planner_external_boundaries(monkeypatch)
    fixed_profile(monkeypatch)
    with planner_session() as session:
        task = seed_comment_task(session, mode="comment", target_count=3)
        _enable_grounding_plan(session, task, source_text="")
        channel_comment.build_plan(session, task)
        before = channel_comment_acceptance(session, task)
        obligations = list(session.scalars(select(CommentFulfillmentObligation)))
        for obligation in obligations:
            action = session.get(Action, obligation.current_action_id)
            action.status = "success"
            action.payload = {
                **action.payload,
                "content_source": "comment_unicode_emoji_fallback",
                "comment_text": "👍",
            }
            obligation.remote_comment_id = f"fallback-{obligation.target_ordinal}"
            obligation.status = "confirmed"
        session.flush()
        after = channel_comment_acceptance(session, task)

    assert before["grounding_quality_status"] == "at_risk"
    assert before["quality_target_not_applicable_ordinal_count"] == 0
    assert after["grounding_quality_status"] == "met"
    assert after["grounded_remote_confirmed_count"] == 0
    assert after["planned_fallback_confirmed_count"] == 2
    assert after["acceptance_status"] == "met"


def test_source_edit_can_promote_planned_fallback_before_gateway(monkeypatch) -> None:
    forbid_planner_external_boundaries(monkeypatch)
    fixed_profile(monkeypatch)
    with planner_session() as session:
        task = seed_comment_task(session, mode="comment", target_count=3)
        _enable_grounding_plan(session, task, source_text="")
        channel_comment.build_plan(session, task)
        old_actions = list(session.scalars(select(Action).where(Action.task_id == task.id)))
        edited = _edited_source(session, text="频道事实正文", source_id="quality-rich-source-2")
        reconcile_channel_comment_source_edit(
            session, session.get(ChannelMessage, 41), edited, at=STABLE_PLANNER_NOW,
        )
        created = channel_comment.build_plan(session, task)
        assignments = list(session.scalars(select(ChannelCommentGroundingAssignment).where(
            ChannelCommentGroundingAssignment.assignment_state == "active",
        )))
        new_actions = list(session.scalars(select(Action).where(
            Action.task_id == task.id, Action.status != "cancelled",
        )))
        plan = session.scalar(select(ChannelCommentPlanContract))
        target = session.get(
            ChannelCommentQualityTargetRevision, plan.current_quality_target_revision_id,
        )

    assert created == 2
    assert all(row.status == "cancelled" for row in old_actions)
    assert len(assignments) == 2
    assert target.quality_target_revision == 2
    assert target.aggregate_grounding_required_count == 2
    assert target.aggregate_planned_fallback_count == 0
    assert {row.payload["source_revision_id"] for row in new_actions} == {edited.id}
    assert {row.payload["quality_target_revision_id"] for row in new_actions} == {target.id}
    assert {row.payload["comment_fallback_intent_kind"] for row in new_actions} == {"emergency"}


def _empty_edited_source(session) -> ChannelMessageSourceRevision:
    return _edited_source(session, text="", source_id="quality-source-revision-2")


def _edited_source(session, *, text: str, source_id: str) -> ChannelMessageSourceRevision:
    message = session.get(ChannelMessage, 41)
    source = ChannelMessageSourceRevision(
        id=source_id, tenant_id=1,
        channel_message_id=message.id, source_revision=2,
        source_remote_message_id=message.message_id,
        source_published_at=message.published_at,
        source_observed_at=STABLE_PLANNER_NOW, source_text_snapshot=text,
        source_content_hash="c" * 64, observation_identity_hash="d" * 64,
        source_operation="edited",
    )
    session.add(source)
    session.flush()
    message.current_source_revision_id = source.id
    return source


def _owned_ordinals(components: list[dict]) -> list[int]:
    return sorted(
        ordinal
        for component in components
        for ordinal in component["owned_ordinal_ids"]
    )

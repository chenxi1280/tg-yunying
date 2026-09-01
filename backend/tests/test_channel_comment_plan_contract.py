from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.models import (
    Action,
    AccountPacingReservation,
    ChannelCommentCapacityAllocationEpoch,
    ChannelCommentContentRevisionOperation,
    ChannelCommentGroundingAssignment,
    ChannelCommentPlanLifecycleEvent,
    ChannelCommentOrdinalAccountBinding,
    ChannelCommentPlanContract,
    ChannelMessage,
    ChannelMessageSourceRevision,
    CommentFulfillmentObligation,
    ExecutionAttempt,
    GenerationJob,
    SourcePacingAdmission,
    SourcePacingState,
    TaskCommentCapacityPeriod,
    TaskCommentCapacityReservation,
)
from app.services.task_center.executors import channel_comment
from app.services.task_center.channel_comment_acceptance import channel_comment_acceptance
from app.services.task_center.channel_comment_capacity import (
    mark_comment_capacity_gateway_hold,
    remaining_comment_capacity,
    reserve_comment_capacity,
    settle_comment_capacity,
)
from app.services.task_center.channel_comment_capacity_allocation import (
    rebalance_comment_capacity_epoch,
)
from app.services.task_center.channel_comment_grounding_guard import comment_grounding_send_blocker
from app.services.task_center.channel_comment_content_revision import (
    reconcile_channel_comment_source_edit,
)
from app.services.task_center.channel_comment_source_delete import (
    settle_channel_comment_source_deleted,
)
from app.services.task_center.service import pause_task
from app.services.task_center.channel_payloads import PostCommentPayload
from channel_comment_planner_test_support import (
    STABLE_PLANNER_NOW,
    fixed_profile,
    forbid_planner_external_boundaries,
    planner_session,
    seed_comment_task,
)


pytestmark = pytest.mark.no_postgres


def _enable_grounding_plan(session, task, *, source_text: str = "频道事实正文") -> None:
    task.fulfillment_contract_version = "fact_first_v3"
    task.type_config = {
        **task.type_config,
        "rolling_window_days": 3,
        "daily_comment_cap": 10,
        "channel_comment_grounding_v1_enabled": True,
        "unicode_emoji_enabled": True,
        "image_meme_enabled": False,
        "unicode_emoji_weight_bps": 10000,
        "image_meme_weight_bps": 0,
        "context_bound_schedule_window_seconds": 3 * 24 * 60 * 60,
    }
    message = session.get(ChannelMessage, 41)
    message.content_preview = source_text
    message.published_at = datetime(2030, 8, 1, 10, 0, 0)
    revision = ChannelMessageSourceRevision(
        id="source-revision-1",
        tenant_id=1,
        channel_message_id=message.id,
        source_revision=1,
        source_remote_message_id=message.message_id,
        source_published_at=message.published_at,
        source_observed_at=STABLE_PLANNER_NOW,
        source_text_snapshot=source_text,
        source_content_hash="a" * 64,
        observation_identity_hash="b" * 64,
        source_operation="observed",
    )
    session.add(revision)
    session.flush()
    message.current_source_revision_id = revision.id
    session.commit()


def test_grounding_plan_freezes_distinct_target_and_survives_config_revision(monkeypatch):
    forbid_planner_external_boundaries(monkeypatch)
    fixed_profile(monkeypatch)
    with planner_session() as session:
        task = seed_comment_task(session, mode="comment", target_count=99)
        _enable_grounding_plan(session, task)

        created = channel_comment.build_plan(session, task)
        plan = session.scalar(select(ChannelCommentPlanContract))
        obligation_count = session.scalar(select(func.count(CommentFulfillmentObligation.id)))
        binding_accounts = list(session.scalars(
            select(ChannelCommentOrdinalAccountBinding.account_id)
            .order_by(ChannelCommentOrdinalAccountBinding.target_ordinal)
        ))
        assignments = list(session.scalars(
            select(ChannelCommentGroundingAssignment)
            .order_by(ChannelCommentGroundingAssignment.target_ordinal)
        ))
        actions = list(session.scalars(select(Action).where(Action.task_id == task.id)))

        task.config_revision = 2
        task.type_config = {**task.type_config, "target_comments_per_message": 1}
        session.commit()
        created_after_edit = channel_comment.build_plan(session, task)

        assert created == plan.required_distinct_account_count == 2
        assert obligation_count == 2
        assert len(binding_accounts) == len(set(binding_accounts)) == 2
        assert len(assignments) == 2
        assert {action.payload["source_revision_id"] for action in actions} == {
            "source-revision-1",
        }
        assert all(action.payload["grounding_assignment_id"] for action in actions)
        assert created_after_edit == 0
        assert session.scalar(select(func.count(ChannelCommentPlanContract.id))) == 1
        assert session.scalar(select(func.count(CommentFulfillmentObligation.id))) == 2


def test_empty_source_freezes_planned_fallback_actions(monkeypatch):
    forbid_planner_external_boundaries(monkeypatch)
    fixed_profile(monkeypatch)
    with planner_session() as session:
        task = seed_comment_task(session, mode="comment", target_count=3)
        _enable_grounding_plan(session, task, source_text="")

        created = channel_comment.build_plan(session, task)
        plan = session.scalar(select(ChannelCommentPlanContract))
        actions = list(session.scalars(select(Action).where(Action.task_id == task.id)))

    assert created == 2
    assert plan.grounding_required_count == 0
    assert plan.planned_fallback_count == 2
    assert {action.payload["comment_fallback_intent_kind"] for action in actions} == {"planned"}
    assert {action.payload["grounding_assignment_id"] for action in actions} == {""}


def test_daily_cap_applies_per_continuous_period_without_shrinking_obligations(monkeypatch):
    forbid_planner_external_boundaries(monkeypatch)
    fixed_profile(monkeypatch)
    with planner_session() as session:
        task = seed_comment_task(session, mode="comment", target_count=3)
        _enable_grounding_plan(session, task)
        task.type_config = {**task.type_config, "daily_comment_cap": 1}
        session.commit()

        created = channel_comment.build_plan(session, task)
        obligations = session.scalar(select(func.count(CommentFulfillmentObligation.id)))
        action_rows = list(session.scalars(select(Action).where(Action.task_id == task.id)))
        reservations = list(session.scalars(select(TaskCommentCapacityReservation)))
        periods = list(session.scalars(select(TaskCommentCapacityPeriod)))

    assert created == len(action_rows) == 2
    assert obligations == 2
    assert len(reservations) == len(periods) == 2
    assert {period.capacity_limit for period in periods} == {1}
    assert {row.capacity_period_id for row in reservations} == {
        period.id for period in periods
    }
    assert {row.reservation_state for row in reservations} == {"action_reserved"}


def test_capacity_reservation_advances_through_gateway_and_remote_fact(monkeypatch):
    forbid_planner_external_boundaries(monkeypatch)
    fixed_profile(monkeypatch)
    with planner_session() as session:
        task = seed_comment_task(session, mode="comment", target_count=3)
        _enable_grounding_plan(session, task)
        channel_comment.build_plan(session, task)
        action = session.scalar(select(Action).where(Action.task_id == task.id))

        mark_comment_capacity_gateway_hold(session, action.id)
        reservation = session.scalar(select(TaskCommentCapacityReservation).where(
            TaskCommentCapacityReservation.action_id == action.id,
        ))
        assert reservation.reservation_state == "gateway_hold"

        settle_comment_capacity(session, reservation.obligation_id, confirmed=True)
        assert reservation.reservation_state == "confirmed"


def test_acceptance_requires_typed_fact_and_grounding_evidence(monkeypatch):
    forbid_planner_external_boundaries(monkeypatch)
    fixed_profile(monkeypatch)
    with planner_session() as session:
        task = seed_comment_task(session, mode="comment", target_count=3)
        _enable_grounding_plan(session, task)
        channel_comment.build_plan(session, task)
        obligations = list(session.scalars(select(CommentFulfillmentObligation)))
        for obligation in obligations:
            action = session.get(Action, obligation.current_action_id)
            action.status = "success"
            action.payload = {
                **action.payload,
                "content_source": "normal",
                "comment_text": "频道事实正文",
            }
            action.result = {
                "comment_quality_audit": {
                    "two_stage_evaluator_evidence": {
                        "semantic_review": {"decision": "pass"},
                    },
                },
            }
            obligation.remote_comment_id = f"remote-{obligation.target_ordinal}"
            obligation.status = "confirmed"
        session.flush()

        acceptance = channel_comment_acceptance(session, task)

    assert acceptance["quantity_status"] == "met"
    assert acceptance["content_mix_status"] == "met"
    assert acceptance["grounding_quality_status"] == "met"
    assert acceptance["acceptance_status"] == "met"


def test_timezone_change_creates_contiguous_capacity_periods() -> None:
    with planner_session() as session:
        task = seed_comment_task(session, mode="comment", target_count=3)
        task.timezone = "Asia/Shanghai"
        session.flush()
        assert remaining_comment_capacity(
            session, task, 10, at=STABLE_PLANNER_NOW,
        ) == 10
        first = session.scalar(select(TaskCommentCapacityPeriod))

        task.timezone = "America/New_York"
        next_at = first.period_end_at.replace(tzinfo=None)
        assert remaining_comment_capacity(
            session, task, 10, at=next_at,
        ) >= 0
        periods = list(session.scalars(
            select(TaskCommentCapacityPeriod)
            .order_by(TaskCommentCapacityPeriod.period_start_at)
        ))

    assert len(periods) == 2
    assert periods[1].period_start_at == periods[0].period_end_at
    assert periods[1].calendar_revision == 2
    assert periods[1].capacity_limit <= 10


@pytest.mark.parametrize(
    ("occupied_offset_hours", "candidate_offset_hours", "expected_reserved"),
    ((0, 23, False), (23, 0, False), (0, 24, True)),
)
def test_rolling_24h_cap_spans_capacity_periods(
    monkeypatch,
    occupied_offset_hours: int,
    candidate_offset_hours: int,
    expected_reserved: bool,
) -> None:
    forbid_planner_external_boundaries(monkeypatch)
    fixed_profile(monkeypatch)
    with planner_session() as session:
        task = seed_comment_task(session, mode="comment", target_count=3)
        _enable_grounding_plan(session, task)
        task.type_config = {**task.type_config, "daily_comment_cap": 1}
        session.commit()
        channel_comment.build_plan(session, task)
        reservations = list(session.scalars(
            select(TaskCommentCapacityReservation)
            .order_by(TaskCommentCapacityReservation.scheduled_for_at)
        ))
        first, second = reservations
        first.scheduled_for_at = STABLE_PLANNER_NOW + timedelta(
            hours=occupied_offset_hours,
        )
        second.reservation_state = "released"
        session.flush()
        obligation = session.get(CommentFulfillmentObligation, second.obligation_id)

        reserved = reserve_comment_capacity(
            session,
            task,
            obligation,
            scheduled_at=STABLE_PLANNER_NOW + timedelta(
                hours=candidate_offset_hours,
            ),
            daily_cap=1,
        )

        assert (reserved is not None) is expected_reserved
        assert second.reservation_state == (
            "plan_reserved" if expected_reserved else "released"
        )
        assert session.scalar(select(func.count(TaskCommentCapacityReservation.id))) == 2


def _add_second_open_plan(session, task, *, due_at: datetime) -> ChannelCommentPlanContract:
    message = ChannelMessage(
        id=42, tenant_id=1, channel_target_id=31, message_id=9002,
        content_preview="第二条频道事实", comment_available=True,
        published_at=datetime(2030, 8, 1, 10, 0, 0),
    )
    session.add(message)
    session.flush()
    first = session.scalar(select(ChannelCommentPlanContract))
    values = {
        column.name: getattr(first, column.name)
        for column in ChannelCommentPlanContract.__table__.columns
        if column.name not in {"id", "channel_message_id", "source_revision_id"}
    }
    revision = ChannelMessageSourceRevision(
        id="source-revision-2", tenant_id=1, channel_message_id=42,
        source_revision=1, source_remote_message_id=9002,
        source_published_at=message.published_at,
        source_observed_at=STABLE_PLANNER_NOW,
        source_text_snapshot="第二条频道事实", source_content_hash="c" * 64,
        observation_identity_hash="d" * 64, source_operation="observed",
    )
    session.add(revision)
    session.flush()
    plan = ChannelCommentPlanContract(
        **values, channel_message_id=42, source_revision_id=revision.id,
    )
    session.add(plan)
    session.flush()
    session.add_all([
        CommentFulfillmentObligation(
            tenant_id=task.tenant_id, task_id=task.id, channel_message_id=42,
            comment_plan_revision=1, target_ordinal=ordinal,
            plan_contract_id=plan.id, status="open", pacing_due_at=due_at,
        )
        for ordinal in (1, 2)
    ])
    session.flush()
    return plan


def test_new_open_plan_shares_future_capacity_by_max_min_round(monkeypatch) -> None:
    forbid_planner_external_boundaries(monkeypatch)
    fixed_profile(monkeypatch)
    with planner_session() as session:
        task = seed_comment_task(session, mode="comment", target_count=3)
        _enable_grounding_plan(session, task)
        channel_comment.build_plan(session, task)
        due_at = STABLE_PLANNER_NOW + timedelta(hours=1)
        first_plan = session.scalar(select(ChannelCommentPlanContract))
        for row in session.scalars(select(TaskCommentCapacityReservation)):
            row.action_id = None
            row.reservation_state = "plan_reserved"
            row.scheduled_for_at = due_at
            obligation = session.get(CommentFulfillmentObligation, row.obligation_id)
            obligation.current_action_id = None
            obligation.status = "open"
            obligation.pacing_due_at = due_at
            obligation.release_not_before_at = due_at
        session.scalar(select(TaskCommentCapacityPeriod)).capacity_limit = 2
        second_plan = _add_second_open_plan(session, task, due_at=due_at)
        second_plan.deadline_at = first_plan.deadline_at + timedelta(hours=1)
        epoch = rebalance_comment_capacity_epoch(
            session, task, daily_cap=2, at=STABLE_PLANNER_NOW,
        )
        reserved_plan_ids = list(session.scalars(select(
            TaskCommentCapacityReservation.plan_contract_id,
        ).where(TaskCommentCapacityReservation.reservation_state == "plan_reserved")))

        assert sorted(reserved_plan_ids) == sorted([
            first_plan.id, second_plan.id,
        ])
        assert epoch.allocation_epoch == 2
        assert session.scalar(select(func.count(CommentFulfillmentObligation.id))) == 4
        assert task.stats["daily_cap_unallocated"] == 2
        assert session.scalar(select(func.count(ChannelCommentCapacityAllocationEpoch.id))) == 2


def test_rebalance_never_moves_gateway_or_confirmed_capacity(monkeypatch) -> None:
    forbid_planner_external_boundaries(monkeypatch)
    fixed_profile(monkeypatch)
    with planner_session() as session:
        task = seed_comment_task(session, mode="comment", target_count=3)
        _enable_grounding_plan(session, task)
        channel_comment.build_plan(session, task)
        reservations = list(session.scalars(select(TaskCommentCapacityReservation)))
        reservations[0].reservation_state = "gateway_hold"
        reservations[1].reservation_state = "confirmed"
        frozen = [
            (row.id, row.action_id, row.scheduled_for_at, row.allocation_epoch)
            for row in reservations
        ]
        _add_second_open_plan(
            session, task, due_at=STABLE_PLANNER_NOW + timedelta(hours=1),
        )

        rebalance_comment_capacity_epoch(
            session, task, daily_cap=3, at=STABLE_PLANNER_NOW,
        )
        immutable = list(session.scalars(select(TaskCommentCapacityReservation).where(
            TaskCommentCapacityReservation.reservation_state.in_({
                "gateway_hold", "confirmed",
            }),
        )))

        assert [
            (row.id, row.action_id, row.scheduled_for_at, row.allocation_epoch)
            for row in immutable
        ] == frozen


def _ordered_obligations(session):
    return list(session.scalars(
        select(CommentFulfillmentObligation).order_by(
            CommentFulfillmentObligation.target_ordinal,
        )
    ))


def _obligation_shape(rows) -> list[tuple]:
    return [
        (
            row.target_ordinal, row.account_id, row.relation_kind,
            row.pacing_due_at, row.release_not_before_at,
        )
        for row in rows
    ]


def _new_edited_source(session, message) -> ChannelMessageSourceRevision:
    edited = ChannelMessageSourceRevision(
        id="source-revision-2", tenant_id=1, channel_message_id=message.id,
        source_revision=2, source_remote_message_id=message.message_id,
        source_published_at=message.published_at,
        source_observed_at=STABLE_PLANNER_NOW,
        source_text_snapshot="编辑后的频道事实",
        source_content_hash="c" * 64, observation_identity_hash="d" * 64,
        source_operation="edited",
    )
    session.add(edited)
    session.flush()
    message.current_source_revision_id = edited.id
    session.flush()
    return edited


def _prepare_source_edit_case(session, task):
    channel_comment.build_plan(session, task)
    actions = sorted(
        session.scalars(select(Action).where(Action.task_id == task.id)),
        key=lambda row: int(row.payload["target_ordinal"]),
    )
    action, held_action = actions
    obligations = _ordered_obligations(session)
    held = next(row for row in obligations if row.current_action_id == held_action.id)
    movable = next(row for row in obligations if row.current_action_id == action.id)
    session.add(GenerationJob(
        id="source-edit-generation-job",
        tenant_id=task.tenant_id,
        task_id=task.id,
        obligation_type="post_comment",
        obligation_id=movable.id,
        generation_sequence=1,
        context_snapshot_version=1,
        state="pending",
    ))
    held_action.status = "unknown_after_send"
    mark_comment_capacity_gateway_hold(session, held_action.id)
    edited = _new_edited_source(session, session.get(ChannelMessage, 41))
    return action, held_action, edited, _obligation_shape(obligations), held


def test_source_edit_replaces_only_pre_gateway_assignment(monkeypatch) -> None:
    forbid_planner_external_boundaries(monkeypatch)
    fixed_profile(monkeypatch)
    with planner_session() as session:
        task = seed_comment_task(session, mode="comment", target_count=3)
        _enable_grounding_plan(session, task)
        action, held_action, edited, original, held_obligation = (
            _prepare_source_edit_case(session, task)
        )
        held_assignment_id = held_obligation.grounding_assignment_id
        held_payload = dict(held_action.payload)
        message = session.get(ChannelMessage, 41)
        operation = reconcile_channel_comment_source_edit(
            session, message, edited, at=STABLE_PLANNER_NOW,
        )[0]
        replay = reconcile_channel_comment_source_edit(
            session, message, edited, at=STABLE_PLANNER_NOW,
        )[0]

        blocker = comment_grounding_send_blocker(
            session, action, PostCommentPayload.model_validate(action.payload),
        )

        session.flush()
        refreshed = _ordered_obligations(session)
        movable = next(row for row in refreshed if row.current_action_id is None)
        successor = session.get(
            ChannelCommentGroundingAssignment, movable.grounding_assignment_id,
        )
        held = next(row for row in refreshed if row.current_action_id == held_action.id)
        reservation = session.scalar(select(TaskCommentCapacityReservation).where(
            TaskCommentCapacityReservation.obligation_id == movable.id,
        ))
        after = _obligation_shape(refreshed)
        operation_count = session.scalar(select(
            func.count(ChannelCommentContentRevisionOperation.id),
        ))
        generation_job = session.get(GenerationJob, "source-edit-generation-job")
        pacing_reservation = session.scalar(select(AccountPacingReservation).where(
            AccountPacingReservation.pacing_slot_key == f"comment:{movable.id}",
        ))

    assert blocker == "source_revision_superseded_before_gateway"
    assert replay.id == operation.id
    assert after == original
    assert movable.status == "replan_required"
    assert successor.source_revision_id == edited.id
    assert successor.supersedes_assignment_id == action.payload["grounding_assignment_id"]
    assert successor.assignment_version == 2
    assert reservation.reservation_state == "released"
    assert action.status == "cancelled"
    assert pacing_reservation.state == "reserved"
    assert pacing_reservation.action_id is None
    assert generation_job.state == "failed"
    assert generation_job.evaluator_evidence == {
        "invalidation_reason": "source_revision_superseded_before_gateway",
    }
    assert held.grounding_assignment_id == held_assignment_id
    assert held_action.payload == held_payload
    assert operation_count == 1


@pytest.mark.parametrize("held_state", ("unknown", "confirmed"))
def test_source_delete_terminates_only_pre_gateway_owner(
    monkeypatch,
    held_state: str,
) -> None:
    forbid_planner_external_boundaries(monkeypatch)
    fixed_profile(monkeypatch)
    with planner_session() as session:
        task = seed_comment_task(session, mode="comment", target_count=3)
        _enable_grounding_plan(session, task)
        channel_comment.build_plan(session, task)
        actions = sorted(
            session.scalars(select(Action).where(Action.task_id == task.id)),
            key=lambda row: int(row.payload["target_ordinal"]),
        )
        movable, held_action = actions
        obligations = _ordered_obligations(session)
        movable_obligation = next(row for row in obligations if row.current_action_id == movable.id)
        held_obligation = next(row for row in obligations if row.current_action_id == held_action.id)
        _seed_pre_gateway_generation_and_source_admission(
            session, task, action=movable, obligation=movable_obligation,
        )
        _hold_delete_identity(
            session, held_action, held_obligation, held_state=held_state,
        )
        frozen_held = (
            dict(held_action.payload), held_obligation.grounding_assignment_id,
            held_obligation.remote_comment_id,
        )
        message = session.get(ChannelMessage, 41)

        event = settle_channel_comment_source_deleted(
            session, message,
            occurred_at=STABLE_PLANNER_NOW,
            evidence_hash="e" * 64,
        )[0]
        replay = settle_channel_comment_source_deleted(
            session, message,
            occurred_at=STABLE_PLANNER_NOW,
            evidence_hash="e" * 64,
        )[0]
        blocker = comment_grounding_send_blocker(
            session, movable, PostCommentPayload.model_validate(movable.payload),
        )
        result = _source_delete_result(
            session, movable_obligation, held_obligation,
        )

    assert replay.id == event.id
    assert blocker == "source_deleted_before_send"
    assert result["plan_state"] == "terminated_source_deleted"
    assert result["movable_status"] == "terminated"
    assert result["movable_action_status"] == "cancelled"
    assert result["generation_state"] == "failed"
    assert result["capacity_state"] == "released"
    assert result["pacing_action_id"] is None
    assert result["source_admission_state"] == "cancelled_pre_gateway"
    assert result["held_identity"] == frozen_held
    assert result["held_capacity_state"] == (
        "confirmed" if held_state == "confirmed" else "gateway_hold"
    )
    assert result["event_count"] == 1


def test_task_pause_releases_only_pre_gateway_comment_owner(monkeypatch) -> None:
    forbid_planner_external_boundaries(monkeypatch)
    fixed_profile(monkeypatch)
    with planner_session() as session:
        task = seed_comment_task(session, mode="comment", target_count=3)
        _enable_grounding_plan(session, task)
        channel_comment.build_plan(session, task)
        actions = sorted(
            session.scalars(select(Action).where(Action.task_id == task.id)),
            key=lambda row: int(row.payload["target_ordinal"]),
        )
        movable, held_action = actions
        obligations = _ordered_obligations(session)
        movable_obligation = next(row for row in obligations if row.current_action_id == movable.id)
        held_obligation = next(row for row in obligations if row.current_action_id == held_action.id)
        _seed_pre_gateway_generation_and_source_admission(
            session, task, action=movable, obligation=movable_obligation,
        )
        _hold_delete_identity(session, held_action, held_obligation, held_state="unknown")
        old_deadline = session.get(
            ChannelCommentPlanContract, movable_obligation.plan_contract_id,
        ).deadline_at
        old_epoch = task.task_lifecycle_epoch

        paused = pause_task(session, task.tenant_id, task.id, "operator")
        edited = _new_edited_source(session, session.get(ChannelMessage, 41))
        reconcile_channel_comment_source_edit(
            session, session.get(ChannelMessage, 41), edited, at=STABLE_PLANNER_NOW,
        )
        replay = pause_task(session, task.tenant_id, task.id, "operator")
        created_while_paused = channel_comment.build_plan(session, replay)
        result = _task_pause_result(session, movable_obligation.id, held_obligation.id)

    assert paused.status == "paused"
    assert paused.task_lifecycle_epoch == old_epoch + 1
    assert replay.task_lifecycle_epoch == paused.task_lifecycle_epoch
    assert created_while_paused == 0
    assert result["deadline"] == old_deadline
    assert result["movable_status"] == "paused_unallocated"
    assert result["movable_action_status"] == "cancelled"
    assert result["generation_state"] == "failed"
    assert result["capacity_state"] == "released"
    assert result["pacing_action_id"] is None
    assert result["source_admission_state"] == "cancelled_pre_gateway"
    assert result["held_status"] == "unknown"
    assert result["held_action_status"] == "unknown_after_send"
    assert result["held_capacity_state"] == "gateway_hold"
    assert result["event_types"] == ["pause"]
    assert result["allocation_epochs"] == [1, 2]
    assert result["assignment_source_revision"] == "source-revision-2"


def test_task_pause_preserves_gateway_started_and_settles_expired(monkeypatch) -> None:
    forbid_planner_external_boundaries(monkeypatch)
    fixed_profile(monkeypatch)
    with planner_session() as session:
        task = seed_comment_task(session, mode="comment", target_count=3)
        _enable_grounding_plan(session, task)
        channel_comment.build_plan(session, task)
        actions = sorted(
            session.scalars(select(Action).where(Action.task_id == task.id)),
            key=lambda row: int(row.payload["target_ordinal"]),
        )
        gateway_action, expired_action = actions
        gateway_action_id = gateway_action.id
        obligations = _ordered_obligations(session)
        gateway_obligation = next(
            row for row in obligations if row.current_action_id == gateway_action.id
        )
        expired_obligation = next(
            row for row in obligations if row.current_action_id == expired_action.id
        )
        plan = session.get(ChannelCommentPlanContract, gateway_obligation.plan_contract_id)
        after_deadline = plan.deadline_at + timedelta(seconds=1)
        gateway_action.status = "executing"
        session.add(ExecutionAttempt(
            id="pause-gateway-attempt", tenant_id=task.tenant_id,
            action_id=gateway_action.id, worker_id="worker", attempt_no=1,
            status="gateway_call_started", before_call_at=after_deadline,
            gateway_call_started_at=after_deadline,
        ))
        monkeypatch.setattr(
            "app.services.task_center.service._now", lambda: after_deadline,
        )

        pause_task(session, task.tenant_id, task.id, "operator")
        gateway_capacity = session.scalar(select(TaskCommentCapacityReservation).where(
            TaskCommentCapacityReservation.obligation_id == gateway_obligation.id,
        ))
        expired_capacity = session.scalar(select(TaskCommentCapacityReservation).where(
            TaskCommentCapacityReservation.obligation_id == expired_obligation.id,
        ))
        result = {
            "gateway_action_status": gateway_action.status,
            "gateway_obligation_status": gateway_obligation.status,
            "gateway_action_id": gateway_obligation.current_action_id,
            "gateway_capacity": gateway_capacity.reservation_state,
            "expired_obligation_status": expired_obligation.status,
            "expired_action_id": expired_obligation.current_action_id,
            "expired_action_status": expired_action.status,
            "expired_capacity": expired_capacity.reservation_state,
        }

    assert result["gateway_action_status"] == "executing"
    assert result["gateway_obligation_status"] == "pending"
    assert result["gateway_action_id"] == gateway_action_id
    assert result["gateway_capacity"] == "gateway_hold"
    assert result["expired_obligation_status"] == "missed_task_paused"
    assert result["expired_action_id"] is None
    assert result["expired_action_status"] == "cancelled"
    assert result["expired_capacity"] == "released"


def _task_pause_result(session, movable_id: str, held_id: str) -> dict:
    movable = session.get(CommentFulfillmentObligation, movable_id)
    held = session.get(CommentFulfillmentObligation, held_id)
    capacity = session.scalar(select(TaskCommentCapacityReservation).where(
        TaskCommentCapacityReservation.obligation_id == movable.id,
    ))
    held_capacity = session.scalar(select(TaskCommentCapacityReservation).where(
        TaskCommentCapacityReservation.obligation_id == held.id,
    ))
    pacing = session.scalar(select(AccountPacingReservation).where(
        AccountPacingReservation.pacing_slot_key == f"comment:{movable.id}",
    ))
    return {
        "deadline": session.get(ChannelCommentPlanContract, movable.plan_contract_id).deadline_at,
        "movable_status": movable.status,
        "movable_action_status": session.get(Action, capacity.action_id).status,
        "generation_state": session.get(GenerationJob, "source-delete-generation-job").state,
        "capacity_state": capacity.reservation_state,
        "pacing_action_id": pacing.action_id,
        "source_admission_state": session.get(
            SourcePacingAdmission, "source-delete-admission",
        ).state,
        "held_status": held.status,
        "held_action_status": session.get(Action, held.current_action_id).status,
        "held_capacity_state": held_capacity.reservation_state,
        "event_types": list(session.scalars(select(
            ChannelCommentPlanLifecycleEvent.event_type,
        ).order_by(ChannelCommentPlanLifecycleEvent.event_type))),
        "allocation_epochs": list(session.scalars(select(
            ChannelCommentCapacityAllocationEpoch.allocation_epoch,
        ).order_by(ChannelCommentCapacityAllocationEpoch.allocation_epoch))),
        "assignment_source_revision": session.get(
            ChannelCommentGroundingAssignment, movable.grounding_assignment_id,
        ).source_revision_id,
    }


def _seed_pre_gateway_generation_and_source_admission(
    session,
    task,
    *,
    action,
    obligation,
) -> None:
    session.add(GenerationJob(
        id="source-delete-generation-job", tenant_id=task.tenant_id,
        task_id=task.id, obligation_type="post_comment",
        obligation_id=obligation.id, generation_sequence=1,
        context_snapshot_version=1, state="pending",
    ))
    state = SourcePacingState(
        id="source-delete-pacing-state", tenant_id=task.tenant_id,
        pacing_domain="comment", source_key_hash="s" * 64,
    )
    session.add(state)
    session.flush()
    session.add(SourcePacingAdmission(
        id="source-delete-admission", admission_key="source-delete-admission",
        tenant_id=task.tenant_id, task_id=task.id,
        source_pacing_state_id=state.id, owner_type="comment_obligation",
        owner_id=obligation.id, action_id=action.id,
        pacing_period_key="message:41", pacing_plan_hash="p" * 64,
        planned_release_at=STABLE_PLANNER_NOW,
        call_not_before_at=STABLE_PLANNER_NOW, source_gap_seconds=1,
        state="reserved",
    ))


def _hold_delete_identity(
    session,
    action,
    obligation,
    *,
    held_state: str,
) -> None:
    if held_state == "confirmed":
        action.status = "success"
        obligation.status = "confirmed"
        obligation.remote_comment_id = "remote-confirmed"
        settle_comment_capacity(session, obligation.id, confirmed=True)
        return
    action.status = "unknown_after_send"
    obligation.status = "unknown"
    mark_comment_capacity_gateway_hold(session, action.id)


def _source_delete_result(session, movable, held) -> dict:
    plan = session.get(ChannelCommentPlanContract, movable.plan_contract_id)
    capacity = session.scalar(select(TaskCommentCapacityReservation).where(
        TaskCommentCapacityReservation.obligation_id == movable.id,
    ))
    held_capacity = session.scalar(select(TaskCommentCapacityReservation).where(
        TaskCommentCapacityReservation.obligation_id == held.id,
    ))
    pacing = session.scalar(select(AccountPacingReservation).where(
        AccountPacingReservation.pacing_slot_key == f"comment:{movable.id}",
    ))
    admission = session.get(SourcePacingAdmission, "source-delete-admission")
    generation = session.get(GenerationJob, "source-delete-generation-job")
    action = session.get(Action, capacity.action_id)
    held_action = session.get(Action, held.current_action_id)
    return {
        "plan_state": plan.contract_state,
        "movable_status": movable.status,
        "movable_action_status": action.status,
        "generation_state": generation.state,
        "capacity_state": capacity.reservation_state,
        "pacing_action_id": pacing.action_id,
        "source_admission_state": admission.state,
        "held_identity": (
            dict(held_action.payload), held.grounding_assignment_id,
            held.remote_comment_id,
        ),
        "held_capacity_state": held_capacity.reservation_state,
        "event_count": session.scalar(select(func.count(
            ChannelCommentPlanLifecycleEvent.id,
        ))),
    }


def test_released_capacity_blocks_frozen_action_before_gateway(monkeypatch) -> None:
    forbid_planner_external_boundaries(monkeypatch)
    fixed_profile(monkeypatch)
    with planner_session() as session:
        task = seed_comment_task(session, mode="comment", target_count=3)
        _enable_grounding_plan(session, task)
        channel_comment.build_plan(session, task)
        action = session.scalar(select(Action).where(Action.task_id == task.id))
        reservation = session.scalar(select(TaskCommentCapacityReservation).where(
            TaskCommentCapacityReservation.action_id == action.id,
        ))
        reservation.reservation_state = "released"
        session.flush()

        blocker = comment_grounding_send_blocker(
            session, action, PostCommentPayload.model_validate(action.payload),
        )

    assert blocker == "comment_capacity_reservation_not_action_reserved"

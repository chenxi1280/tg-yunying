from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.models import (
    Action,
    AccountPacingReservation,
    AiContentPolicyVersion,
    AiProvider,
    ChannelCommentCapacityAllocationEpoch,
    ChannelCommentContentRevisionOperation,
    ChannelCommentGroundingAssignment,
    ChannelCommentGroundingEvaluation,
    ChannelCommentGroundingSnapshot,
    ChannelCommentPlanLifecycleEvent,
    ChannelCommentOrdinalAccountBinding,
    ChannelCommentPlanContract,
    ChannelDiscussionGroupBinding,
    ChannelDiscussionThreadBinding,
    ChannelMessage,
    ChannelMessageComment,
    ChannelMessageSourceRevision,
    CommentFulfillmentObligation,
    ExecutionAttempt,
    GenerationJob,
    OperationTarget,
    SourcePacingAdmission,
    SourcePacingState,
    TaskCommentCapacityPeriod,
    TaskCommentCapacityReservation,
    TenantAiProviderRouteItem,
    TenantAiProviderRouteSet,
)
from app.services.task_center.channel_comment_discussion_contracts import (
    AUTHORITATIVE_GROUP_STAGE,
    AUTHORITATIVE_THREAD_STAGE,
    EnrollmentRequest,
    GroupProbeObservation,
    MembershipObservation,
    ThreadProbeObservation,
    record_group_probe,
    record_membership_fact,
    record_thread_probe,
)
from app.services.task_center.channel_comment_grounding_enrollment import activate_grounding_enrollment
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
from app.services.task_center.channel_comment_grounding_evaluation import (
    evaluate_grounding_claims,
    persist_grounding_evaluation,
)
from app.services.task_center.channel_comment_grounding_read_model import (
    channel_comment_grounding_read_model,
)
from app.services.task_center import dispatcher
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


def _enable_grounding_plan(
    session,
    task,
    *,
    source_text: str = "糖糖老师 今日主推黑丝 下午可约",
    business_max: int = 80,
    fallback_max_bps: int = 2000,
) -> None:
    task.fulfillment_contract_version = "fact_first_v3"
    task.type_config = {**task.type_config, **_grounding_config(
        business_max=business_max, fallback_max_bps=fallback_max_bps,
    )}
    _seed_ai_content_runtime(session)
    message = session.get(ChannelMessage, 41)
    message.content_preview = source_text
    message.published_at = datetime(2030, 8, 1, 10, 0, 0)
    binding = _seed_discussion_binding(session, task)
    revision = _new_source_revision(message, source_text=source_text, binding=binding)
    session.add(revision)
    session.flush()
    thread = _seed_discussion_thread(session, revision, binding)
    revision.discussion_thread_binding_id = thread.id
    revision.discussion_thread_revision = thread.thread_revision
    revision.discussion_thread_identity_hash = thread.identity_hash
    _seed_discussion_memberships(session, binding)
    _activate_test_enrollment(session, task, message=message, binding=binding)
    message.current_source_revision_id = revision.id
    session.commit()


def _grounding_config(*, business_max: int, fallback_max_bps: int) -> dict:
    return {
        "rolling_window_days": 3,
        "daily_comment_cap": 10,
        "business_max_comments_per_message": business_max,
        "planned_fallback_max_bps": fallback_max_bps,
        "channel_comment_grounding_v1_enabled": True,
        "ai_two_stage_enabled": True,
        "ai_content_route_v2_enabled": True,
        "ai_model": "comment-generator",
        "ai_semantic_reviewer_model": "comment-reviewer",
        "ai_content_policy_version_id": "policy-v1",
        "ai_content_allowed_routes": ["general"],
        "unicode_emoji_enabled": True,
        "image_meme_enabled": False,
        "unicode_emoji_weight_bps": 10000,
        "image_meme_weight_bps": 0,
        "context_bound_schedule_window_seconds": 3 * 24 * 60 * 60,
    }


def _new_source_revision(message, *, source_text: str, binding) -> ChannelMessageSourceRevision:
    return ChannelMessageSourceRevision(
        id="source-revision-1",
        tenant_id=1,
        channel_target_id=message.channel_target_id,
        channel_message_id=message.id,
        source_revision=1,
        source_remote_message_id=message.message_id,
        source_published_at=message.published_at,
        source_published_at_fact_id=f"telegram_message_date:{message.channel_target_id}:{message.message_id}",
        source_observed_at=STABLE_PLANNER_NOW,
        source_type="message_text",
        source_text_snapshot=source_text,
        source_content_hash=hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
        observation_identity_hash="b" * 64,
        source_length=len(source_text),
        captured_length=len(source_text),
        truncation_state="complete",
        source_operation="observed",
        discussion_group_binding_id=binding.id,
        discussion_group_binding_revision=binding.binding_revision,
        discussion_group_identity_hash=binding.identity_hash,
    )


def _activate_test_enrollment(session, task, *, message, binding) -> None:
    activate_grounding_enrollment(
        session,
        EnrollmentRequest(
            tenant_id=task.tenant_id,
            task_id=task.id,
            expected_config_revision=task.config_revision,
            expected_lifecycle_epoch=task.task_lifecycle_epoch,
            group_binding_id=binding.id,
            enabled_at=message.published_at - timedelta(minutes=1),
            operator_id="test-operator",
            approval_reference="test-approval",
        ),
    )


def _seed_ai_content_runtime(session) -> None:
    if session.get(AiContentPolicyVersion, "policy-v1") is None:
        session.add(AiContentPolicyVersion(
            id="policy-v1", tenant_id=1, version=1, status="active",
            policy_hash="e" * 64, approved_by="test-operator",
            route_rules={"allowed_routes": ["general"]},
        ))
    providers = (
        (201, "comment-generator", "mock://generator"),
        (202, "comment-reviewer", "mock://reviewer"),
    )
    for provider_id, model, base_url in providers:
        if session.get(AiProvider, provider_id) is None:
            session.add(AiProvider(
                id=provider_id, provider_name=f"provider-{provider_id}",
                base_url=base_url, model_name=model,
                api_key_ciphertext="ciphertext", credential_enabled=True,
            ))
    session.flush()
    purposes = (
        ("comment_context_route", 201, "comment-generator"),
        ("comment_realize_general", 201, "comment-generator"),
        ("comment_semantic_review", 202, "comment-reviewer"),
    )
    for revision, (purpose, provider_id, model) in enumerate(purposes, 1):
        route = TenantAiProviderRouteSet(
            tenant_id=1, purpose=purpose, revision=1, status="active",
            content_hash=str(revision) * 64,
        )
        session.add(route)
        session.flush()
        session.add(TenantAiProviderRouteItem(
            route_set_id=route.id, priority=1,
            provider_id=provider_id, model_name=model,
        ))


def _seed_discussion_binding(session, task) -> ChannelDiscussionGroupBinding:
    if session.get(OperationTarget, 32) is None:
        session.add(OperationTarget(
            id=32, tenant_id=1, target_type="group", tg_peer_id="-10032",
            title="测试频道讨论组", can_send=True, auth_status="已授权运营",
        ))
        session.flush()
    return record_group_probe(session, GroupProbeObservation(
        tenant_id=1,
        channel_target_id=31,
        target_reference_revision=1,
        channel_peer_id="-10031",
        discussion_target_id=32,
        discussion_peer_id="-10032",
        probe_request_id=f"probe-{task.id}",
        probe_status="success",
        probe_stage=AUTHORITATIVE_GROUP_STAGE,
        observed_at=STABLE_PLANNER_NOW,
        fresh_until_at=STABLE_PLANNER_NOW + timedelta(days=1),
    ))


def _seed_discussion_thread(session, revision, binding) -> ChannelDiscussionThreadBinding:
    return record_thread_probe(session, ThreadProbeObservation(
        tenant_id=1,
        source_revision_id=revision.id,
        group_binding_id=binding.id,
        probe_request_id=f"thread-{revision.id}",
        probe_status="success",
        probe_stage=AUTHORITATIVE_THREAD_STAGE,
        observed_at=STABLE_PLANNER_NOW,
        fresh_until_at=STABLE_PLANNER_NOW + timedelta(days=1),
        discussion_peer_id="-10032",
        thread_root_message_id=7001,
    ))


def _seed_discussion_memberships(session, binding) -> None:
    for account_id in (101, 102, 103):
        record_membership_fact(session, MembershipObservation(
            tenant_id=1,
            account_id=account_id,
            group_binding_id=binding.id,
            discussion_peer_id="-10032",
            membership_status="already_joined",
            can_send=True,
            observed_at=STABLE_PLANNER_NOW,
            fresh_until_at=STABLE_PLANNER_NOW + timedelta(days=1),
        ))


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


def test_empty_source_blocks_when_planned_fallback_exceeds_business_cap(monkeypatch):
    forbid_planner_external_boundaries(monkeypatch)
    fixed_profile(monkeypatch)
    with planner_session() as session:
        task = seed_comment_task(session, mode="comment", target_count=3)
        _enable_grounding_plan(session, task, source_text="")

        created = channel_comment.build_plan(session, task)
        plan = session.scalar(select(ChannelCommentPlanContract))
        actions = list(session.scalars(select(Action).where(Action.task_id == task.id)))
        obligations = list(session.scalars(select(CommentFulfillmentObligation)))
        acceptance = channel_comment_acceptance(session, task)

    assert created == 0
    assert plan.grounding_required_count == 0
    assert plan.planned_fallback_count == 2
    assert actions == obligations == []
    assert task.last_error == "channel_comment_planned_fallback_cap_exceeded"
    assert acceptance["fallback_business_state"] == "cap_exceeded"
    assert acceptance["acceptance_status"] == "blocked"


def test_planned_fallback_action_keeps_exact_grounding_snapshot_identity(monkeypatch):
    forbid_planner_external_boundaries(monkeypatch)
    fixed_profile(monkeypatch)
    with planner_session() as session:
        task = seed_comment_task(session, mode="comment", target_count=3)
        _enable_grounding_plan(
            session, task, source_text="", fallback_max_bps=10000,
        )

        created = channel_comment.build_plan(session, task)
        snapshot = session.scalar(select(ChannelCommentGroundingSnapshot))
        actions = list(session.scalars(select(Action).where(Action.task_id == task.id)))

    assert created == 2
    assert snapshot.source_state == "insufficient"
    assert actions
    assert all(not row.payload["grounding_assignment_id"] for row in actions)
    assert all(row.payload["grounding_snapshot_id"] == snapshot.id for row in actions)
    assert all(row.payload["comment_grounding_revision"] == 1 for row in actions)
    assert all(
        row.payload["grounding_evidence_hash"] == snapshot.source_content_hash
        for row in actions
    )


def test_business_cap_freezes_uncapped_demand_without_claiming_ratio_met(monkeypatch):
    forbid_planner_external_boundaries(monkeypatch)
    fixed_profile(monkeypatch)
    with planner_session() as session:
        task = seed_comment_task(session, mode="comment", target_count=99)
        _enable_grounding_plan(session, task, business_max=1)

        created = channel_comment.build_plan(session, task)
        plan = session.scalar(select(ChannelCommentPlanContract))
        acceptance = channel_comment_acceptance(session, task)

    assert created == 1
    assert plan.uncapped_required_distinct_account_count == 2
    assert plan.required_distinct_account_count == 1
    assert plan.business_max_comments_per_message == 1
    assert plan.business_cap_state == "business_cap_adjusted"
    assert acceptance["quantity_uncapped_target_count"] == 2
    assert acceptance["business_cap_adjusted_count"] == 1


def test_grounding_mixed_reply_shortfall_blocks_without_direct_downgrade(monkeypatch):
    forbid_planner_external_boundaries(monkeypatch)
    fixed_profile(monkeypatch)
    with planner_session() as session:
        task = seed_comment_task(session, mode="mixed", reply_min=2, target_count=3)
        _enable_grounding_plan(session, task)
        reply = session.scalar(select(ChannelMessageComment).where(
            ChannelMessageComment.comment_message_id == 8102,
        ))
        session.delete(reply)
        session.flush()

        created = channel_comment.build_plan(session, task)
        actions = list(session.scalars(select(Action).where(Action.task_id == task.id)))
        obligations = list(session.scalars(select(CommentFulfillmentObligation)))

    assert created == 0
    assert actions == obligations == []
    assert task.last_error == "channel_comment_reply_target_shortfall"


def test_grounding_reply_slot_rejects_planned_fallback(monkeypatch):
    forbid_planner_external_boundaries(monkeypatch)
    fixed_profile(monkeypatch)
    with planner_session() as session:
        task = seed_comment_task(
            session,
            mode="reply",
            reply_min=2,
            requested_reply_ids=[8101, 8102],
            target_count=3,
        )
        _enable_grounding_plan(session, task, source_text="", fallback_max_bps=10000)

        created = channel_comment.build_plan(session, task)
        actions = list(session.scalars(select(Action).where(Action.task_id == task.id)))
        obligations = list(session.scalars(select(CommentFulfillmentObligation)))

    assert created == 0
    assert actions == obligations == []
    assert task.last_error == "channel_comment_reply_fallback_forbidden"


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
            assignment = session.get(
                ChannelCommentGroundingAssignment,
                obligation.grounding_assignment_id,
            )
            action.status = "success"
            action.payload = {
                **action.payload,
                "content_source": "normal",
                "comment_text": (
                    f"{assignment.teacher_name} {assignment.primary_aspect_text}"
                ).strip(),
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


def test_plan_freezes_snapshot_and_assignments_reference_exact_evidence(monkeypatch) -> None:
    forbid_planner_external_boundaries(monkeypatch)
    fixed_profile(monkeypatch)
    with planner_session() as session:
        task = seed_comment_task(session, mode="comment", target_count=3)
        _enable_grounding_plan(session, task)
        channel_comment.build_plan(session, task)

        snapshot = session.scalar(select(ChannelCommentGroundingSnapshot))
        assignments = list(session.scalars(select(ChannelCommentGroundingAssignment)))

        assert snapshot.comment_grounding_revision == 1
        assert snapshot.source_state == "ready"
        assert snapshot.teacher_candidates_json
        assert snapshot.aspect_evidence_json
        evidence_ids = {row["evidence_id"] for row in snapshot.aspect_evidence_json}
        assert assignments
        assert all(row.grounding_snapshot_id == snapshot.id for row in assignments)
        assert all(row.primary_evidence_id in evidence_ids for row in assignments)
        assert all(row.relation_kind == "direct" for row in assignments)

        read_model = channel_comment_grounding_read_model(session, task)
        assert read_model["messages"][0]["snapshot_id"] == snapshot.id
        assert len(read_model["slots"]) == 2
        assert {row["lifecycle_state"] for row in read_model["slots"]} == {
            "pending_generation",
        }


def test_grounding_evaluation_rejects_unsupported_experience_and_is_append_only(
    monkeypatch,
) -> None:
    forbid_planner_external_boundaries(monkeypatch)
    fixed_profile(monkeypatch)
    with planner_session() as session:
        task = seed_comment_task(session, mode="comment", target_count=3)
        _enable_grounding_plan(session, task)
        channel_comment.build_plan(session, task)
        action = session.scalar(select(Action).where(Action.action_type == "post_comment"))
        payload = PostCommentPayload.model_validate(action.payload)
        assignment = session.get(
            ChannelCommentGroundingAssignment, payload.grounding_assignment_id,
        )
        accepted = f"{assignment.teacher_name} {assignment.primary_aspect_text}".strip()

        pass_decision = evaluate_grounding_claims(
            session, action, payload, content=accepted,
        )
        rejected = evaluate_grounding_claims(
            session, action, payload, content=f"我去过 {accepted}",
        )
        candidate_hash = hashlib.sha256(accepted.encode("utf-8")).hexdigest()
        evaluation = persist_grounding_evaluation(
            session, action, payload,
            candidate_hash=candidate_hash,
            claim_results=list(pass_decision.claim_results),
            semantic_evidence={
                "decision": "pass", "model": "reviewer",
                "prompt_version": "semantic_reviewer_v1",
                "primary_aspect_result": "pass",
            },
            final_result="pass",
        )
        session.flush()

        assert pass_decision.allowed is True
        assert rejected.code == "unsupported_claim"
        assert evaluation.final_result == "pass"
        assert session.scalar(select(ChannelCommentGroundingEvaluation)).id == evaluation.id


def test_grounding_evaluation_cannot_pass_when_reply_relation_is_unknown(monkeypatch) -> None:
    forbid_planner_external_boundaries(monkeypatch)
    fixed_profile(monkeypatch)
    with planner_session() as session:
        task = seed_comment_task(session, mode="comment", target_count=3)
        _enable_grounding_plan(session, task)
        channel_comment.build_plan(session, task)
        action = session.scalar(select(Action).where(Action.action_type == "post_comment"))
        payload = PostCommentPayload.model_validate(action.payload).model_copy(update={
            "comment_mode": "reply",
            "reply_to_message_id": 8101,
        })
        evaluation = persist_grounding_evaluation(
            session,
            action,
            payload,
            candidate_hash="b" * 64,
            claim_results=[],
            semantic_evidence={
                "decision": "pass",
                "primary_aspect_result": "pass",
                "reply_relation_result": "unknown",
            },
            final_result="pass",
        )

        assert evaluation.primary_aspect_result == "pass"
        assert evaluation.reply_relation_result == "unknown"
        assert evaluation.final_result == "unknown"


def test_v12_outbound_text_hash_is_recomputed_before_gateway(monkeypatch) -> None:
    forbid_planner_external_boundaries(monkeypatch)
    fixed_profile(monkeypatch)
    with planner_session() as session:
        task = seed_comment_task(session, mode="comment", target_count=3)
        _enable_grounding_plan(session, task)
        channel_comment.build_plan(session, task)
        action = session.scalar(select(Action).where(Action.action_type == "post_comment"))
        payload = PostCommentPayload.model_validate(action.payload)
        text = payload.grounding_primary_aspect_text
        content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        accepted = payload.model_copy(update={
            "comment_text": text,
            "ai_generation_status": "ready",
            "comment_lifecycle_state": "quality_accepted",
            "accepted_content_text": text,
            "accepted_content_hash": content_hash,
            "quality_contract_version": "channel_comment_grounding_quality_v1",
        })
        action.candidate_hash = content_hash

        assert dispatcher._comment_outbound_hash_blocker(action, accepted) == ""
        tampered = accepted.model_copy(update={"comment_text": f"{text} "})
        assert dispatcher._comment_outbound_hash_blocker(
            action, tampered,
        ) == "grounding_outbound_content_mismatch"


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
    source_text = "妮妮老师 主推水疗 今晚可约"
    edited = ChannelMessageSourceRevision(
        id="source-revision-2", tenant_id=1,
        channel_target_id=message.channel_target_id, channel_message_id=message.id,
        source_revision=2, source_remote_message_id=message.message_id,
        source_published_at=message.published_at,
        source_published_at_fact_id=f"telegram_message_date:{message.channel_target_id}:{message.message_id}",
        source_observed_at=STABLE_PLANNER_NOW,
        source_type="message_text", source_text_snapshot=source_text,
        source_content_hash=hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
        observation_identity_hash="d" * 64,
        source_length=len(source_text), captured_length=len(source_text),
        truncation_state="complete",
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

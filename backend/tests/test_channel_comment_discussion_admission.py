from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.integrations.telegram.contracts import OperationResult
from app.models import (
    Action,
    ChannelCommentPlanContract,
    ChannelDiscussionGroupBinding,
    CommentFulfillmentObligation,
    DiscussionMembershipFact,
    ExecutionAttempt,
    TgAccount,
)
from app.services.task_center import dispatcher
from app.services.task_center.channel_comment_discussion_contracts import (
    MembershipObservation,
    record_membership_fact,
)
from app.services.task_center.channel_comment_discussion_admission import (
    discussion_admission_candidate_ids,
    ensure_discussion_membership_actions,
)
from app.services.task_center.executors import channel_comment
from app.services.task_center.payloads import EnsureDiscussionMembershipPayload
from app.services.task_center.remote_reconcile_business_facts import (
    apply_confirmed_business_fact,
    typed_remote_fact_id,
)
from app.schemas.task_center import ChannelCommentConfig
from channel_comment_planner_test_support import (
    fixed_profile,
    forbid_planner_external_boundaries,
    planner_session,
    seed_comment_task,
)
from test_channel_comment_plan_contract import _enable_grounding_plan


pytestmark = pytest.mark.no_postgres


def _grounding_config_with_auto_join() -> dict:
    return {
        "target_channel_id": 31,
        "ai_model": "generator-model",
        "ai_two_stage_enabled": True,
        "ai_semantic_reviewer_model": "reviewer-model",
        "ai_content_route_v2_enabled": True,
        "ai_content_policy_version_id": "policy-v1",
        "ai_content_allowed_routes": ["general"],
        "channel_comment_grounding_v1_enabled": True,
        "daily_comment_cap": 10,
        "auto_join_discussion_enabled": True,
        "discussion_join_account_ids": [101],
        "discussion_join_budget": 1,
        "discussion_join_pacing_policy_version": "discussion_join_pacing_v1",
        "discussion_join_pacing_policy": {"interval_seconds": 60},
    }


def test_auto_join_requires_explicit_pacing_version_and_positive_interval() -> None:
    config = _grounding_config_with_auto_join()
    ChannelCommentConfig(**config)
    with pytest.raises(ValueError, match="discussion_join_pacing_policy_version_required"):
        ChannelCommentConfig(**{**config, "discussion_join_pacing_policy_version": ""})
    with pytest.raises(ValueError, match="discussion_join_pacing_policy_required"):
        ChannelCommentConfig(**{
            **config, "discussion_join_pacing_policy": {"interval_seconds": 0},
        })


def _mark_accounts_not_joined(session, binding_id: str) -> None:
    now_value = datetime.now(timezone.utc)
    for account_id in (101, 102, 103):
        record_membership_fact(session, MembershipObservation(
            tenant_id=1,
            account_id=account_id,
            group_binding_id=binding_id,
            discussion_peer_id="-10032",
            membership_status="not_participant",
            can_send=False,
            observed_at=now_value,
            fresh_until_at=now_value + timedelta(hours=1),
        ))
    session.commit()


def _enable_auto_join(task) -> None:
    task.type_config = {
        **task.type_config,
        "auto_join_discussion_enabled": True,
        "discussion_join_account_ids": [101, 102, 103],
        "discussion_join_budget": 3,
        "discussion_join_pacing_policy_version": "discussion_join_pacing_v1",
        "discussion_join_pacing_policy": {"interval_seconds": 60},
    }


def _discussion_scope(session):
    binding_id = session.scalar(select(DiscussionMembershipFact.group_binding_id))
    binding = session.get(ChannelDiscussionGroupBinding, binding_id)
    accounts = [session.get(TgAccount, account_id) for account_id in (101, 102, 103)]
    return binding_id, binding, accounts


def _record_membership(session, binding_id: str, account_id: int, status: str, now_value) -> None:
    record_membership_fact(session, MembershipObservation(
        tenant_id=1, account_id=account_id, group_binding_id=binding_id,
        discussion_peer_id="-10032", membership_status=status,
        can_send=status in {"joined", "already_joined"}, observed_at=now_value,
        fresh_until_at=now_value + timedelta(hours=1),
    ))


def test_auto_join_disabled_freezes_zero_eligible_plan_without_join_action(monkeypatch) -> None:
    forbid_planner_external_boundaries(monkeypatch)
    fixed_profile(monkeypatch)
    with planner_session() as session:
        task = seed_comment_task(session, mode="comment", target_count=3)
        _enable_grounding_plan(session, task)
        binding_id = session.scalar(select(DiscussionMembershipFact.group_binding_id))
        _mark_accounts_not_joined(session, binding_id)

        created = channel_comment.build_plan(session, task)
        plan = session.scalar(select(ChannelCommentPlanContract))

        assert created == 0
        assert plan is not None
        assert plan.eligibility_snapshot_state == "no_eligible_accounts"
        assert session.scalar(select(Action).where(
            Action.action_type == "ensure_discussion_membership",
        )) is None


def test_join_budget_applies_after_ready_and_forbidden_accounts_are_removed() -> None:
    with planner_session() as session:
        task = seed_comment_task(session, mode="comment", target_count=3)
        _enable_grounding_plan(session, task)
        binding_id, binding, accounts = _discussion_scope(session)
        now_value = datetime.now(timezone.utc)
        _record_membership(session, binding_id, 102, "not_participant", now_value)
        _record_membership(session, binding_id, 103, "restricted", now_value)
        _enable_auto_join(task)
        task.type_config = {**task.type_config, "discussion_join_budget": 1}
        session.flush()

        candidates = discussion_admission_candidate_ids(
            session, task, binding, accounts=accounts, now_value=now_value,
        )

        assert candidates == frozenset({102})


def test_unknown_membership_and_terminal_join_are_not_admission_capacity() -> None:
    with planner_session() as session:
        task = seed_comment_task(session, mode="comment", target_count=3)
        _enable_grounding_plan(session, task)
        binding_id = session.scalar(select(DiscussionMembershipFact.group_binding_id))
        _mark_accounts_not_joined(session, binding_id)
        _enable_auto_join(task)
        now_value = datetime.now(timezone.utc)
        _, binding, accounts = _discussion_scope(session)
        actions = ensure_discussion_membership_actions(
            session, task, binding, accounts=accounts, now_value=now_value,
        )
        actions[101].status = "unknown_after_send"
        actions[102].status = "failed"
        _record_membership(session, binding_id, 101, "unknown", now_value)
        session.flush()

        candidates = discussion_admission_candidate_ids(
            session, task, binding, accounts=accounts, now_value=now_value,
        )
        reserved = ensure_discussion_membership_actions(
            session, task, binding, accounts=accounts, now_value=now_value,
        )

        assert 101 not in candidates
        assert 102 not in candidates
        assert set(reserved) == {103}


def test_join_success_unlocks_frozen_plan_without_recount(monkeypatch) -> None:
    forbid_planner_external_boundaries(monkeypatch)
    fixed_profile(monkeypatch)
    monkeypatch.setattr(
        dispatcher.gateway, "ensure_channel_membership",
        lambda *_args, **_kwargs: OperationResult(True, detail="joined"),
    )
    monkeypatch.setattr(
        dispatcher.gateway, "probe_target_capabilities",
        lambda *_args, **_kwargs: OperationResult(True, detail="can_send"),
    )
    with planner_session() as session:
        task = seed_comment_task(session, mode="comment", target_count=3)
        _enable_grounding_plan(session, task)
        binding_id = session.scalar(select(DiscussionMembershipFact.group_binding_id))
        _mark_accounts_not_joined(session, binding_id)
        _enable_auto_join(task)
        session.commit()

        assert channel_comment.build_plan(session, task) == 0
        join_actions = list(session.scalars(select(Action).where(
            Action.action_type == "ensure_discussion_membership",
        )))
        required_before = len(join_actions)
        for action in join_actions:
            account = session.get(TgAccount, action.account_id)
            payload = EnsureDiscussionMembershipPayload.model_validate(action.payload)
            assert dispatcher._dispatch_discussion_membership(
                session, action, account=account, credentials=None, payload=payload,
            )
            session.commit()

        created = channel_comment.build_plan(session, task)
        obligations = list(session.scalars(select(CommentFulfillmentObligation)))
        duplicate_joins = list(session.scalars(select(Action).where(
            Action.action_type == "ensure_discussion_membership",
        )))

        assert created == len(obligations) == required_before
        assert len(duplicate_joins) == required_before
        assert all(action.status == "success" for action in join_actions)
        assert all(
            (action.result or {}).get("discussion_membership_remote_fact", {}).get("can_send")
            for action in join_actions
        )


def test_join_scope_drift_is_gateway_zero_call(monkeypatch) -> None:
    calls: list[str] = []
    forbid_planner_external_boundaries(monkeypatch)
    fixed_profile(monkeypatch)
    monkeypatch.setattr(
        dispatcher.gateway, "ensure_channel_membership",
        lambda *_args, **_kwargs: calls.append("join") or OperationResult(True),
    )
    with planner_session() as session:
        task = seed_comment_task(session, mode="comment", target_count=3)
        _enable_grounding_plan(session, task)
        binding_id = session.scalar(select(DiscussionMembershipFact.group_binding_id))
        _mark_accounts_not_joined(session, binding_id)
        _enable_auto_join(task)
        session.commit()
        channel_comment.build_plan(session, task)
        action = session.scalar(select(Action).where(
            Action.action_type == "ensure_discussion_membership",
        ))
        task.config_revision += 1
        session.commit()

        payload = EnsureDiscussionMembershipPayload.model_validate(action.payload)
        account = session.get(TgAccount, action.account_id)
        dispatcher._dispatch_discussion_membership(
            session, action, account=account, credentials=None, payload=payload,
        )

        assert calls == []
        assert action.status == "failed"
        assert (action.result or {}).get("error_code") == "discussion_membership_config_drift"


def test_join_target_gate_blocks_before_attempt_and_gateway(monkeypatch) -> None:
    from app.services.outbound_target_gate import OutboundGateBlock

    calls: list[str] = []
    forbid_planner_external_boundaries(monkeypatch)
    fixed_profile(monkeypatch)
    monkeypatch.setattr(
        "app.services.outbound_target_gate.evaluate_outbound_target_gate",
        lambda *_args, **_kwargs: OutboundGateBlock(
            "target_reference_superseded", "目标引用已变化",
        ),
    )
    monkeypatch.setattr(
        dispatcher.gateway, "ensure_channel_membership",
        lambda *_args, **_kwargs: calls.append("join") or OperationResult(True),
    )
    with planner_session() as session:
        task = seed_comment_task(session, mode="comment", target_count=3)
        _enable_grounding_plan(session, task)
        binding_id = session.scalar(select(DiscussionMembershipFact.group_binding_id))
        _mark_accounts_not_joined(session, binding_id)
        _enable_auto_join(task)
        session.commit()
        channel_comment.build_plan(session, task)
        action = session.scalar(select(Action).where(
            Action.action_type == "ensure_discussion_membership",
        ))

        dispatcher._dispatch_discussion_membership(
            session, action, account=session.get(TgAccount, action.account_id),
            credentials=None,
            payload=EnsureDiscussionMembershipPayload.model_validate(action.payload),
        )

        assert calls == []
        assert session.scalar(select(ExecutionAttempt).where(
            ExecutionAttempt.action_id == action.id,
        )) is None
        assert action.status == "skipped"


def test_join_remote_reconcile_restores_typed_membership_fact(monkeypatch) -> None:
    forbid_planner_external_boundaries(monkeypatch)
    fixed_profile(monkeypatch)
    with planner_session() as session:
        task = seed_comment_task(session, mode="comment", target_count=3)
        _enable_grounding_plan(session, task)
        binding_id = session.scalar(select(DiscussionMembershipFact.group_binding_id))
        _mark_accounts_not_joined(session, binding_id)
        _enable_auto_join(task)
        session.commit()
        channel_comment.build_plan(session, task)
        action = session.scalar(select(Action).where(
            Action.action_type == "ensure_discussion_membership",
        ))
        account = session.get(TgAccount, action.account_id)
        attempt = dispatcher._begin_execution_attempt(session, action, account)
        payload = EnsureDiscussionMembershipPayload.model_validate(action.payload)
        action.result = {"discussion_membership_remote_fact": {
            "account_id": action.account_id,
            "discussion_peer_id": payload.discussion_peer_id,
            "discussion_group_binding_id": payload.discussion_group_binding_id,
            "discussion_group_binding_revision": payload.discussion_group_binding_revision,
            "membership_status": "already_joined",
            "can_send": True,
        }}
        fact_id = typed_remote_fact_id(
            action, attempt, "discussion_membership_observed",
        )

        apply_confirmed_business_fact(
            session, action, attempt,
            result="remote_confirmed", remote_fact_id=fact_id,
        )
        fact = session.scalar(select(DiscussionMembershipFact).where(
            DiscussionMembershipFact.account_id == action.account_id,
            DiscussionMembershipFact.is_current.is_(True),
        ))

        assert fact.membership_status == "already_joined"
        assert fact.can_send is True
        assert action.result["discussion_membership_remote_fact"]["fact_id"] == fact.id
        assert action.result["validation_stage"] == "remote_reconcile_discussion_membership"

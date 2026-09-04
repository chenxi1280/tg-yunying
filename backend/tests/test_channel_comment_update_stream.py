from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    AccountBehaviorBudgetLedger,
    AccountBehaviorBudgetPolicyRevision,
    AccountExternalUseHold,
    ChannelDiscussionGroupBinding,
    ChannelDiscussionThreadBinding,
    ChannelMessage,
    ChannelMessageComment,
    ChannelMessageSourceRevision,
    CommentFulfillmentObligation,
    DiscussionMembershipFact,
    ExecutionAttempt,
    ExternalAccountUsePolicyRevision,
    StageWakeOutbox,
    Task,
    TaskMembershipAdmissionItem,
    TaskPlannerWakeState,
    TelegramAuthorizationUpdateState,
    Tenant,
    TgAccount,
    TgAccountAuthorization,
    UnownedOutboundActivityObservation,
)
from app.models.channel_comment_discussion import (
    ChannelDiscussionGroupProbeEvent,
    ChannelDiscussionThreadProbeEvent,
)
from app.services._common import _now
from app.services.task_center.channel_comment_update_stream import (
    consume_channel_comment_update_deliveries,
    ensure_channel_comment_update_subscription,
)
from app.services.task_center.channel_payloads import PostCommentPayload
from app.services.task_center.payloads import create_comment_action
from app.services.task_center.listener_runtime import _comment_listener_account_id
from app.services.task_center.telegram_update_ingress import (
    NormalizedUpdateIngress,
    ingest_normalized_update,
)


pytestmark = pytest.mark.no_postgres


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return Session(engine)


def _seed(session: Session):
    observed = datetime(2026, 9, 5, 10, 0)
    session.add(Tenant(id=1, name="comment stream"))
    account = TgAccount(
        id=11,
        tenant_id=1,
        display_name="listener",
        phone_masked="11",
        status="在线",
    )
    authorization = TgAccountAuthorization(
        id=21,
        tenant_id=1,
        account_id=11,
        slot_generation=1,
        is_current=True,
        is_slot_current=True,
        status="active",
        session_ciphertext="session",
    )
    channel = _channel_target()
    discussion = _discussion_target()
    task = Task(
        id="comment-stream-task",
        tenant_id=1,
        name="interactive comment",
        type="channel_comment",
        status="running",
        type_config={
            "target_channel_id": 7,
            "engagement_contract_version": "unified_engagement_v1",
        },
    )
    session.add_all([
        account,
        authorization,
        channel,
        discussion,
        task,
        AccountBehaviorBudgetPolicyRevision(
            tenant_id=1,
            account_class="normal",
            action_budgets={"total": 20, "authored_comment": 10},
        ),
        ExternalAccountUsePolicyRevision(
            tenant_id=1,
            hold_seconds_by_class={"authored_comment": 600},
            collision_classes_by_class={
                "authored_comment": ["authored_comment", "reaction"],
            },
        ),
    ])
    session.flush()
    account.current_authorization_id = authorization.id
    message = ChannelMessage(
        id=31,
        tenant_id=1,
        channel_target_id=channel.id,
        message_id=501,
        content_preview="source post",
        published_at=observed,
    )
    session.add(message)
    session.flush()
    source = ChannelMessageSourceRevision(
        id="source-revision",
        tenant_id=1,
        channel_target_id=channel.id,
        channel_message_id=message.id,
        source_revision=1,
        source_remote_message_id=message.message_id,
        source_published_at=observed,
        source_observed_at=observed,
        source_text_snapshot="source post",
        source_content_hash="a" * 64,
        observation_identity_hash="b" * 64,
    )
    probe = ChannelDiscussionGroupProbeEvent(
        id="group-probe",
        tenant_id=1,
        channel_target_id=channel.id,
        target_reference_revision=1,
        probe_request_id="group-probe",
        probe_status="success",
        probe_stage="channels_get_full_channel",
        observed_linked_chat_id="-1008",
        observed_at=observed,
    )
    binding = ChannelDiscussionGroupBinding(
        id="group-binding",
        tenant_id=1,
        channel_target_id=channel.id,
        target_reference_revision=1,
        binding_revision=1,
        channel_peer_id="-1007",
        discussion_target_id=discussion.id,
        discussion_peer_id="-1008",
        identity_hash="c" * 64,
        binding_status="active",
        probe_event_id=probe.id,
        observed_at=observed,
    )
    session.add_all([source, probe, binding])
    session.flush()
    thread_probe = ChannelDiscussionThreadProbeEvent(
        id="thread-probe",
        tenant_id=1,
        source_revision_id=source.id,
        group_binding_id=binding.id,
        probe_request_id="thread-probe",
        probe_status="success",
        probe_stage="discussion_message_lookup",
        observed_thread_root_message_id=900,
        observed_at=observed,
    )
    thread = ChannelDiscussionThreadBinding(
        id="thread-binding",
        tenant_id=1,
        source_revision_id=source.id,
        group_binding_id=binding.id,
        thread_revision=1,
        discussion_peer_id="-1008",
        thread_root_message_id=900,
        identity_hash="d" * 64,
        probe_event_id=thread_probe.id,
        observed_at=observed,
    )
    session.add_all([thread_probe, thread])
    session.flush()
    message.current_source_revision_id = source.id
    source.discussion_group_binding_id = binding.id
    source.discussion_thread_binding_id = thread.id
    assert ensure_channel_comment_update_subscription(
        session,
        task,
        binding,
        listener_account_id=account.id,
    )
    state = session.scalar(select(TelegramAuthorizationUpdateState))
    state.state = "live"
    state.owner_id = "comment-test"
    state.lease_expires_at = _now() + timedelta(minutes=1)
    session.flush()
    return task, binding, state, authorization


def _channel_target():
    from app.models import OperationTarget

    return OperationTarget(
        id=7,
        tenant_id=1,
        target_type="channel",
        tg_peer_id="-1007",
        title="source",
    )


def _discussion_target():
    from app.models import OperationTarget

    return OperationTarget(
        id=8,
        tenant_id=1,
        target_type="group",
        tg_peer_id="-1008",
        title="discussion",
    )


def _ingest(session, state, *, remote_id, content, event_type="message_new", sender_id="77"):
    ingest_normalized_update(
        session,
        state.id,
        NormalizedUpdateIngress(
            update_identity_key=f"{event_type}:{remote_id}:{content}",
            constructor_name="UpdateNewChannelMessage",
            pts_evidence=remote_id,
            pts_count_evidence=1,
            routing_peer_type="channel",
            routing_peer_id="-1008",
            normalized_items=({
                "source_message_id": remote_id,
                "event_type": event_type,
                "sender_peer_type": "user",
                "sender_peer_id": sender_id,
                "sender_name": "真人读者",
                "sender_is_bot": False,
                "reply_to_message_id": 900,
                "source_top_message_id": 900,
                "media_type": "text",
                "content": content,
                "sent_at": datetime(2026, 9, 5, 10, 0).isoformat(),
            },),
        ),
        owner_id="comment-test",
        owner_fencing_epoch=state.owner_fencing_epoch,
    )


def _pending_comment_action(session, task, *, remote_peer="-1008"):
    payload = PostCommentPayload(
        channel_id="-1007",
        channel_target_id=7,
        channel_message_id=31,
        message_id=501,
        actual_target_peer=remote_peer,
        comment_text="",
        ai_generation_status="pending",
        comment_lifecycle_state="pending_generation",
    )
    action = create_comment_action(
        session,
        task,
        11,
        _now() + timedelta(minutes=4),
        payload,
    )
    session.flush()
    return action


def test_discussion_update_persists_human_comment_and_wakes_planner() -> None:
    with _session() as session:
        task, binding, state, _authorization = _seed(session)
        obligation = CommentFulfillmentObligation(
            tenant_id=task.tenant_id,
            task_id=task.id,
            channel_message_id=31,
            comment_plan_revision=1,
            target_ordinal=1,
            relation_kind="direct",
            fallback_intent_kind="emergency",
            status="open",
        )
        session.add(obligation)
        session.flush()
        _ingest(session, state, remote_id=901, content="这个怎么参加？")

        assert consume_channel_comment_update_deliveries(
            session,
            task,
            binding,
        ) == 1
        comment = session.scalar(select(ChannelMessageComment))
        assert comment.comment_message_id == 901
        assert comment.content_preview == "这个怎么参加？"
        assert session.scalar(select(func.count(StageWakeOutbox.id))) == 0
        wake = session.scalar(select(TaskPlannerWakeState))
        assert wake.reason_code == "discussion_comment_update"
        assert obligation.relation_kind == "reply"
        assert obligation.reply_to_message_id == 901
        assert obligation.status == "replan_required"
        assert obligation.release_not_before_at is not None


def test_discussion_ignores_edit_but_delete_invalidates_reply_target() -> None:
    with _session() as session:
        task, binding, state, _authorization = _seed(session)
        obligation = CommentFulfillmentObligation(
            tenant_id=task.tenant_id,
            task_id=task.id,
            channel_message_id=31,
            comment_plan_revision=1,
            target_ordinal=1,
            relation_kind="direct",
            fallback_intent_kind="emergency",
            status="open",
        )
        session.add(obligation)
        session.flush()
        _ingest(session, state, remote_id=902, content="原问题")
        consume_channel_comment_update_deliveries(session, task, binding)
        original_hash = obligation.reply_target_snapshot["content_hash"]
        action = _pending_comment_action(session, task)
        obligation.current_action_id = action.id
        obligation.action_attempt_no = 1
        obligation.status = "pending"
        session.flush()
        _ingest(
            session,
            state,
            remote_id=902,
            content="修改后的问题",
            event_type="message_edit",
        )
        assert consume_channel_comment_update_deliveries(session, task, binding) == 0
        comment = session.scalar(select(ChannelMessageComment))
        assert comment.content_preview == "原问题"
        assert obligation.relation_kind == "reply"
        assert obligation.reply_target_snapshot["content_hash"] == original_hash
        assert action.status == "pending"
        assert obligation.current_action_id == action.id

        _ingest(
            session,
            state,
            remote_id=902,
            content="",
            event_type="message_delete",
        )
        assert consume_channel_comment_update_deliveries(session, task, binding) == 1
        assert comment.content_preview == ""
        assert obligation.relation_kind == "direct"
        assert obligation.reply_to_message_id is None
        assert obligation.status == "replan_required"


def test_managed_account_comment_is_not_treated_as_human() -> None:
    with _session() as session:
        task, binding, state, authorization = _seed(session)
        authorization.telegram_user_id_digest = hashlib.sha256(b"88").hexdigest()
        _ingest(
            session,
            state,
            remote_id=903,
            content="我们自己的评论",
            sender_id="88",
        )

        assert consume_channel_comment_update_deliveries(
            session,
            task,
            binding,
        ) == 0
        assert session.scalar(select(func.count(ChannelMessageComment.id))) == 0
        observation = session.scalar(select(UnownedOutboundActivityObservation))
        hold = session.scalar(select(AccountExternalUseHold))
        ledger = session.scalar(select(AccountBehaviorBudgetLedger))
        assert observation.canonical_source_identity == "thread:900"
        assert hold.canonical_peer_id == "-1008"
        assert ledger.counters["authored_comment"]["unowned"] == 1


def test_human_comment_preempts_pending_direct_action_before_gateway() -> None:
    with _session() as session:
        task, binding, state, _authorization = _seed(session)
        action = _pending_comment_action(session, task)
        obligation = CommentFulfillmentObligation(
            tenant_id=task.tenant_id,
            task_id=task.id,
            channel_message_id=31,
            comment_plan_revision=1,
            target_ordinal=1,
            relation_kind="direct",
            fallback_intent_kind="emergency",
            current_action_id=action.id,
            action_attempt_no=1,
            status="pending",
        )
        session.add(obligation)
        session.flush()
        _ingest(session, state, remote_id=904, content="这个要怎么操作？")

        assert consume_channel_comment_update_deliveries(session, task, binding) == 1
        assert action.status == "cancelled"
        assert action.result["error_code"] == "discussion_response_preempted_direct_before_gateway"
        assert obligation.current_action_id is None
        assert obligation.relation_kind == "reply"
        assert obligation.reply_to_message_id == 904


def test_outbound_message_id_from_other_discussion_does_not_hide_human_comment() -> None:
    with _session() as session:
        task, binding, state, _authorization = _seed(session)
        session.add(ChannelMessageComment(
            tenant_id=task.tenant_id,
            channel_target_id=7,
            channel_message_id=31,
            discussion_peer_id="-100999",
            comment_message_id=905,
            content_preview="旧讨论组里的同号评论",
        ))
        action = _pending_comment_action(session, task, remote_peer="-100999")
        action.status = "success"
        session.add(ExecutionAttempt(
            tenant_id=task.tenant_id,
            action_id=action.id,
            account_id=11,
            status="success",
            remote_message_id="905",
        ))
        session.flush()
        _ingest(session, state, remote_id=905, content="同号但不是我们的评论")

        assert consume_channel_comment_update_deliveries(session, task, binding) == 1
        comments = list(session.scalars(select(ChannelMessageComment).where(
            ChannelMessageComment.comment_message_id == 905,
        )))
        assert len(comments) == 2
        by_peer = {comment.discussion_peer_id: comment for comment in comments}
        assert by_peer["-1008"].content_preview == "同号但不是我们的评论"


def test_comment_listener_rotates_away_from_failed_ready_account() -> None:
    with _session() as session:
        task, binding, _state, _authorization = _seed(session)
        session.add(TgAccount(
            id=12,
            tenant_id=1,
            display_name="standby",
            phone_masked="12",
            status="在线",
        ))
        session.add_all([
            TaskMembershipAdmissionItem(
                tenant_id=1, task_id=task.id, account_id=account_id, target_id=7,
            )
            for account_id in (11, 12)
        ])
        observed = _now()
        session.add_all([
            DiscussionMembershipFact(
                tenant_id=1,
                account_id=account_id,
                discussion_peer_id="-1008",
                group_binding_id=binding.id,
                fact_revision=1,
                membership_status="joined",
                can_send=True,
                observed_at=observed,
                fresh_until_at=observed + timedelta(minutes=10),
            )
            for account_id in (11, 12)
        ])
        task.stats = {
            "comment_update_stream_state": "live",
            "comment_update_stream_listener_account_id": 11,
            "telegram_update_channel_errors": {"-1008": "timeout"},
        }
        session.flush()

        assert _comment_listener_account_id(
            session, task, binding, source_account_id=11,
        ) == 12

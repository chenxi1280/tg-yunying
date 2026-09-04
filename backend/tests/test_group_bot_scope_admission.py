from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.services.task_center import dispatcher
from app.database import Base
from app.models import (
    Action,
    ChannelMessage,
    CommentFulfillmentObligation,
    ExecutionAttempt,
    GroupBotAdmission,
    GroupContextMessage,
    OperationTarget,
    PendingVisibilityCredit,
    PostSendVisibilityObservation,
    Task,
    TaskAccountDailyCoverage,
    TaskMembershipAdmissionItem,
    Tenant,
    TgAccount,
    TgGroup,
)
from app.services.task_center.dispatcher import (
    _action_needs_pending_visibility,
    _group_bot_admission_gate_pass,
    recover_pending_visibility_credits,
)
from app.services.task_center.group_bot_admission import (
    READY_STATE,
    create_policy,
    ensure_admission_after_join,
    ingest_trusted_bot_prompt,
    mark_channel_follow_completed,
    mark_visible_confirmed,
)


pytestmark = pytest.mark.no_postgres


def test_group_send_locks_speaker_state_before_admission(monkeypatch) -> None:
    with _session() as session:
        _seed_scope(session)
        action = session.get(Action, "send-1")
        account = session.get(TgAccount, 11)
        order: list[str] = []
        monkeypatch.setattr(
            dispatcher,
            "_lock_group_ai_speaker_state",
            lambda *_args, **_kwargs: order.append("speaker_state"),
        )
        monkeypatch.setattr(
            dispatcher,
            "_group_bot_admission_gate_pass",
            lambda *_args, **_kwargs: order.append("admission") or False,
        )

        result = dispatcher._prepare_group_send(
            session,
            action,
            dispatcher.SendMessageDispatchContext(
                account=account,
                credentials=object(),
                payload=SimpleNamespace(group_id=7),
            ),
            generation_dependencies=SimpleNamespace(),
        )

        assert result is None
        assert order == ["speaker_state", "admission"]


def test_gateway_gate_backfills_missing_scoped_admission_and_defers_body() -> None:
    with _session() as session:
        _seed_scope(session)
        action = session.get(Action, "send-1")
        action.lease_owner = "old-worker:1"
        action.lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=30)
        action.claim_owner = "old-worker:1:dispatcher"
        action.claim_token = "claim-token"
        action.claim_expires_at = datetime.now(timezone.utc) + timedelta(minutes=1)

        allowed = _group_bot_admission_gate_pass(session, action, group_id=7, account_id=11)

        admission = session.query(GroupBotAdmission).one()
        assert allowed is False
        assert admission.account_id == 11
        assert admission.join_start_cursor == "500"
        assert action.status == "pending"
        assert action.result["error_code"] == "group_bot_admission_wait"
        assert action.result["deferred"] is True
        assert action.executed_at is None
        assert action.lease_owner == ""
        assert action.lease_expires_at is None
        assert action.claim_owner == ""
        assert action.claim_token == ""
        assert action.claim_expires_at is None


def test_gateway_gate_keeps_membership_only_group_outside_admission_flow() -> None:
    with _session() as session:
        _seed_scope(session)
        task = session.get(Task, "task-ai")
        task.type_config = {"target_group_id": 7}
        session.add(
            GroupBotAdmission(
                tenant_id=1,
                group_id=7,
                account_id=11,
                state="observation_stale",
            )
        )
        session.flush()
        action = session.get(Action, "send-1")

        allowed = _group_bot_admission_gate_pass(session, action, group_id=7, account_id=11)

        assert allowed is True
        assert session.query(GroupBotAdmission).count() == 1
        assert _action_needs_pending_visibility(session, action, remote_id="600") is True
        mark_visible_confirmed(session, admission=session.query(GroupBotAdmission).one())
        assert _action_needs_pending_visibility(session, action, remote_id="601") is False


def test_gateway_gate_expands_audited_group_bot_scope_to_membership_account() -> None:
    with _session() as session:
        _seed_scope(session)
        task = session.get(Task, "task-ai")
        task.type_config = {"target_group_id": 7}
        session.add(
            GroupBotAdmission(
                tenant_id=1,
                group_id=7,
                account_id=12,
                state="required_channel_follow_pending",
                trusted_bot_peer_id="900",
                evidence_ref="msg:bot-rule",
            )
        )
        session.flush()
        action = session.get(Action, "send-1")

        allowed = _group_bot_admission_gate_pass(session, action, group_id=7, account_id=11)

        admission = session.scalar(
            select(GroupBotAdmission).where(GroupBotAdmission.account_id == 11)
        )
        assert allowed is False
        assert admission is not None
        assert action.result["group_bot_admission_backfilled"] is True


def test_gateway_gate_blocks_account_after_post_send_intercept() -> None:
    with _session() as session:
        _seed_scope(session)
        task = session.get(Task, "task-ai")
        task.type_config = {"target_group_id": 7}
        session.add(
            GroupBotAdmission(
                tenant_id=1,
                group_id=7,
                account_id=11,
                state="post_send_intercepted",
            )
        )
        session.flush()
        action = session.get(Action, "send-1")

        assert _group_bot_admission_gate_pass(
            session,
            action,
            group_id=7,
            account_id=11,
        ) is False
        assert action.status == "pending"
        assert action.result["error_code"] == "group_bot_admission_wait"


def test_fact_first_send_does_not_reuse_legacy_post_send_intercept(monkeypatch) -> None:
    with _session() as session:
        _seed_scope(session)
        task = session.get(Task, "task-ai")
        task.fulfillment_contract_version = "fact_first_v3"
        action = session.get(Action, "send-1")
        legacy = GroupBotAdmission(
            tenant_id=1,
            group_id=7,
            account_id=11,
            state="post_send_intercepted",
            failure_code="post_send_intercepted",
        )
        session.add(legacy)
        session.flush()
        monkeypatch.setattr(
            dispatcher,
            "_group_send_membership_payload",
            lambda *_args, **_kwargs: None,
        )

        handled = dispatcher._recover_send_message_required_channel(
            session,
            action,
            session.get(TgAccount, 11),
            object(),
            session.get(TgGroup, 7),
            SimpleNamespace(group_id=7),
            SimpleNamespace(
                ok=False,
                failure_type="group_permission_denied",
                detail="需要权限",
            ),
            None,
        )

        assert handled is False
        assert legacy.state == "post_send_intercepted"
        assert (action.result or {}).get("error_code") != "legacy_group_bot_intercepted"


def test_fact_first_action_does_not_open_legacy_visibility_hold() -> None:
    with _session() as session:
        _seed_scope(session)
        task = session.get(Task, "task-ai")
        task.fulfillment_contract_version = "fact_first_v3"
        action = session.get(Action, "send-1")
        session.add(
            GroupBotAdmission(
                tenant_id=1,
                group_id=7,
                account_id=11,
                state="post_follow_visibility_probe",
            )
        )
        session.flush()

        assert _action_needs_pending_visibility(session, action, remote_id="600") is False


def test_unified_fact_first_action_requires_remote_visibility_fact() -> None:
    with _session() as session:
        _seed_scope(session)
        task = session.get(Task, "task-ai")
        task.fulfillment_contract_version = "fact_first_v3"
        task.type_config = {
            **(task.type_config or {}),
            "engagement_contract_version": "unified_engagement_v1",
        }
        action = session.get(Action, "send-1")

        assert _action_needs_pending_visibility(session, action, remote_id="600") is True


def test_post_follow_probe_is_held_until_remote_visibility_confirms() -> None:
    with _session() as session:
        _seed_scope(session)
        action = session.get(Action, "send-1")
        admission = ensure_admission_after_join(
            session,
            tenant_id=1,
            group_id=7,
            account_id=11,
            membership_action_id="join-1",
            join_start_cursor="500",
        )
        ingest_trusted_bot_prompt(
            session,
            admission=admission,
            message_id="bot-1",
            text="请关注 https://t.me/school_news",
            bot_peer_id="900",
            is_admin_bot=True,
        )
        create_policy(
            session,
            tenant_id=1,
            group_id=7,
            completion_policy="explicit_bot_confirmation",
            trusted_bot_peer_id="900",
            reason="visible probe is required",
            evidence_ref="msg:bot-1",
            created_by="operator",
        )
        mark_channel_follow_completed(session, admission=admission, channel_ref="school_news")
        admission.source_message_id = ""

        assert _group_bot_admission_gate_pass(session, action, group_id=7, account_id=11) is True
        assert action.payload["group_bot_post_follow_visibility_probe"] is True
        assert _action_needs_pending_visibility(session, action, remote_id="600") is True
        mark_visible_confirmed(session, admission=admission)
        assert admission.state == READY_STATE


def test_existing_unbound_post_follow_probe_recovers_same_action() -> None:
    with _session() as session:
        _seed_scope(session)
        action = session.get(Action, "send-1")
        admission = ensure_admission_after_join(
            session,
            tenant_id=1,
            group_id=7,
            account_id=11,
            membership_action_id="join-1",
        )
        admission.state = "post_follow_visibility_probe"
        admission.post_send_visibility_state = "pending"

        assert _group_bot_admission_gate_pass(session, action, group_id=7, account_id=11) is True
        assert action.payload["group_bot_post_follow_visibility_probe"] is True
        assert action.payload["group_bot_admission_id"] == admission.id
        assert admission.transport_observation["post_follow_probe_action_id"] == action.id

        session.commit()
        action = session.get(Action, action.id)
        assert _group_bot_admission_gate_pass(session, action, group_id=7, account_id=11) is True


def test_post_follow_probe_rebinds_only_after_pre_gateway_terminal() -> None:
    with _session() as session:
        _seed_scope(session)
        first = session.get(Action, "send-1")
        second = Action(
            id="send-2",
            tenant_id=1,
            task_id="task-ai",
            task_type="group_ai_chat",
            action_type="send_message",
            account_id=11,
            status="pending",
            payload={"group_id": 7},
        )
        session.add(second)
        admission = ensure_admission_after_join(
            session,
            tenant_id=1,
            group_id=7,
            account_id=11,
            membership_action_id="join-1",
        )
        admission.state = "post_follow_visibility_probe"

        assert _group_bot_admission_gate_pass(session, first, group_id=7, account_id=11) is True
        assert _group_bot_admission_gate_pass(session, second, group_id=7, account_id=11) is False

        first.status = "failed"
        assert _group_bot_admission_gate_pass(session, second, group_id=7, account_id=11) is True
        assert admission.transport_observation["post_follow_probe_action_id"] == second.id


def test_post_follow_probe_never_rebinds_after_gateway_started() -> None:
    with _session() as session:
        _seed_scope(session)
        first = session.get(Action, "send-1")
        second = Action(
            id="send-2",
            tenant_id=1,
            task_id="task-ai",
            task_type="group_ai_chat",
            action_type="send_message",
            account_id=11,
            status="pending",
            payload={"group_id": 7},
        )
        session.add(second)
        admission = ensure_admission_after_join(
            session,
            tenant_id=1,
            group_id=7,
            account_id=11,
            membership_action_id="join-1",
        )
        admission.state = "post_follow_visibility_probe"
        assert _group_bot_admission_gate_pass(session, first, group_id=7, account_id=11) is True
        first.status = "failed"
        session.add(
            ExecutionAttempt(
                tenant_id=1,
                action_id=first.id,
                attempt_no=1,
                status="failed",
                gateway_call_started_at=datetime.now(timezone.utc),
            )
        )
        session.flush()

        assert _group_bot_admission_gate_pass(session, second, group_id=7, account_id=11) is False
        assert admission.transport_observation["post_follow_probe_action_id"] == first.id


def test_pending_visibility_recovery_normalizes_aware_created_at() -> None:
    with _session() as session:
        _seed_scope(session)
        action = session.get(Action, "send-1")
        action.status = "unknown_after_send"
        hold = PendingVisibilityCredit(
            tenant_id=1,
            action_id=action.id,
            remote_message_id="",
        )
        session.add(hold)
        session.flush()
        hold.created_at = datetime.now(timezone.utc) - timedelta(minutes=10)

        assert recover_pending_visibility_credits(session) == 1
        assert action.result["pending_visibility_age_seconds"] >= 500
        assert action.result["visibility_status"] == "visibility_observation_unknown"
        assert hold.status == "unknown"


def test_pending_visibility_does_not_confirm_before_full_window(monkeypatch) -> None:
    with _session() as session:
        _seed_scope(session)
        action = session.get(Action, "send-1")
        action.status = "unknown_after_send"
        session.add(
            PendingVisibilityCredit(
                tenant_id=1,
                action_id=action.id,
                remote_message_id="600",
            )
        )
        session.flush()
        calls: list[str] = []
        monkeypatch.setattr(
            dispatcher,
            "_probe_post_send_visibility",
            lambda *_args, **_kwargs: calls.append("probe") or "visible_confirmed",
        )

        assert recover_pending_visibility_credits(session) == 0
        assert calls == []
        assert action.status == "unknown_after_send"


def test_pending_visibility_confirms_after_full_window(monkeypatch) -> None:
    with _session() as session:
        _seed_scope(session)
        action = session.get(Action, "send-1")
        action.status = "unknown_after_send"
        action.payload = {**action.payload, "coverage_ledger_id": "coverage-11"}
        session.add(
            TaskAccountDailyCoverage(
                id="coverage-11",
                tenant_id=1,
                task_id=action.task_id,
                group_id=7,
                account_id=11,
                coverage_date=datetime.now(timezone.utc).date(),
                state="unknown",
                reserved_action_id=action.id,
                last_action_id=action.id,
            )
        )
        session.add(
            ExecutionAttempt(
                id="attempt-600",
                tenant_id=1,
                action_id=action.id,
                account_id=11,
                status="success",
                remote_message_id="600",
                gateway_call_started_at=datetime.now(timezone.utc),
            )
        )
        hold = PendingVisibilityCredit(
            tenant_id=1,
            action_id=action.id,
            remote_message_id="600",
        )
        session.add(hold)
        session.flush()
        hold.created_at = datetime.now(timezone.utc) - timedelta(seconds=100)
        monkeypatch.setattr(
            dispatcher,
            "_probe_post_send_visibility",
            lambda *_args, **_kwargs: "visible_confirmed",
        )

        assert recover_pending_visibility_credits(session) == 1
        assert action.status == "success"
        assert action.result["visibility_status"] == "visible_confirmed"
        coverage = session.get(TaskAccountDailyCoverage, "coverage-11")
        assert coverage.state == "confirmed"
        assert coverage.confirmed_count == 1


def test_pending_visibility_intercept_uses_storage_safe_hold_status(monkeypatch) -> None:
    with _session() as session:
        _seed_scope(session)
        action = session.get(Action, "send-1")
        action.status = "unknown_after_send"
        admission = ensure_admission_after_join(
            session,
            tenant_id=1,
            group_id=7,
            account_id=11,
            membership_action_id="join-1",
            join_start_cursor="500",
        )
        hold = PendingVisibilityCredit(
            tenant_id=1,
            action_id=action.id,
            remote_message_id="600",
        )
        session.add(hold)
        session.flush()
        hold.created_at = datetime.now(timezone.utc) - timedelta(seconds=100)
        monkeypatch.setattr(
            dispatcher,
            "_probe_post_send_visibility",
            lambda *_args, **_kwargs: "not_visible",
        )

        assert recover_pending_visibility_credits(session) == 1
        assert hold.status == "intercepted"
        assert len(hold.status) <= PendingVisibilityCredit.__table__.c.status.type.length
        assert action.status == "failed"
        assert action.result["error_code"] == "post_send_intercepted"
        assert admission.state == "post_send_intercepted"


def test_unified_comment_waits_for_visibility_before_confirming_obligation(
    monkeypatch,
) -> None:
    with _session() as session:
        _seed_scope(session)
        task = Task(
            id="task-comment",
            tenant_id=1,
            name="统一评论",
            type="channel_comment",
            status="running",
            type_config={"engagement_contract_version": "unified_engagement_v1"},
        )
        message = ChannelMessage(
            id=41,
            tenant_id=1,
            channel_target_id=8,
            message_id=9001,
            content_preview="来源消息",
        )
        action = Action(
            id="comment-visibility-action",
            tenant_id=1,
            task_id=task.id,
            task_type="channel_comment",
            action_type="post_comment",
            account_id=11,
            status="success",
            candidate_hash="a" * 64,
            payload={
                "channel_id": "-1008",
                "actual_target_peer": "-1007",
                "comment_fulfillment_obligation_id": "comment-obligation",
            },
        )
        obligation = CommentFulfillmentObligation(
            id="comment-obligation",
            tenant_id=1,
            task_id=task.id,
            channel_message_id=message.id,
            comment_plan_revision=1,
            target_ordinal=1,
            current_action_id=action.id,
            status="pending",
        )
        attempt = ExecutionAttempt(
            id="comment-visibility-attempt",
            tenant_id=1,
            action_id=action.id,
            account_id=11,
            status="success",
            remote_message_id="700",
            gateway_call_started_at=datetime.now(timezone.utc),
        )
        session.add_all([task, message, action, obligation, attempt])
        session.flush()
        assert dispatcher._maybe_hold_pending_visibility(
            action,
            attempt=attempt,
            remote_id="700",
        )
        hold = session.scalar(
            select(PendingVisibilityCredit).where(
                PendingVisibilityCredit.action_id == action.id
            )
        )
        observation = session.scalar(
            select(PostSendVisibilityObservation).where(
                PostSendVisibilityObservation.action_id == action.id
            )
        )
        assert hold is not None
        assert observation is not None
        assert observation.target_peer == "-1007"
        assert observation.accepted_content_hash == "a" * 64
        assert observation.state == "visibility_pending"
        hold.created_at = datetime.now(timezone.utc) - timedelta(seconds=100)
        assert _action_needs_pending_visibility(session, action, remote_id="700")
        monkeypatch.setattr(
            dispatcher,
            "_probe_post_send_visibility",
            lambda *_args, **kwargs: (
                "visible_confirmed"
                if kwargs["target_peer"] == "-1007"
                else ""
            ),
        )

        assert recover_pending_visibility_credits(session) == 1
        assert action.status == "success"
        assert obligation.status == "confirmed"
        assert obligation.remote_comment_id == "700"
        assert obligation.telegram_discussion_peer_id == "-1007"
        assert observation.state == "visible_confirmed"


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return Session(engine)


def _seed_scope(session: Session) -> None:
    session.add_all(
        [
            Tenant(id=1, name="t"),
            TgAccount(id=11, tenant_id=1, display_name="账号甲", phone_masked="+11"),
            TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="群", group_type="supergroup"),
            OperationTarget(id=8, tenant_id=1, target_type="group", tg_peer_id="-1007", title="群"),
            Task(
                id="task-ai",
                tenant_id=1,
                name="ai",
                type="group_ai_chat",
                status="running",
                type_config={"target_group_id": 7, "group_bot_admission_required": True},
            ),
            TaskMembershipAdmissionItem(
                tenant_id=1,
                task_id="task-ai",
                account_id=11,
                target_id=8,
                phase="completed",
            ),
            GroupContextMessage(
                tenant_id=1,
                group_id=7,
                listener_account_id=11,
                sender_peer_id="member",
                content="baseline",
                remote_message_id="500",
                sent_at=datetime.now(timezone.utc),
            ),
            Action(
                id="send-1",
                tenant_id=1,
                task_id="task-ai",
                task_type="group_ai_chat",
                action_type="send_message",
                account_id=11,
                status="executing",
                payload={"group_id": 7},
            ),
        ]
    )
    session.flush()

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.services.task_center import dispatcher
from app.database import Base
from app.models import (
    Action,
    ExecutionAttempt,
    GroupBotAdmission,
    GroupContextMessage,
    OperationTarget,
    PendingVisibilityCredit,
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
        assert action.result["error_code"] == "group_bot_admission_wait"


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

        assert recover_pending_visibility_credits(session) == 0
        assert action.result["pending_visibility_age_seconds"] >= 500


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

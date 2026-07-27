from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    Action,
    GroupBotAdmission,
    GroupContextMessage,
    OperationTarget,
    Task,
    TaskMembershipAdmissionItem,
    Tenant,
    TgAccount,
    TgGroup,
)
from app.services.task_center.dispatcher import (
    _action_needs_pending_visibility,
    _group_bot_admission_gate_pass,
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

        allowed = _group_bot_admission_gate_pass(session, action, group_id=7, account_id=11)

        admission = session.query(GroupBotAdmission).one()
        assert allowed is False
        assert admission.account_id == 11
        assert admission.join_start_cursor == "500"
        assert action.status == "pending"
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

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Action, GroupBotRequiredChannelFollow, GroupContextMessage, Task, Tenant, TgAccount, TgGroup
from app.services.task_center.group_bot_admission import (
    ensure_admission_after_join,
    ingest_trusted_bot_prompt,
    mark_channel_follow_completed,
    plan_required_channel_follow_actions,
)
from app.services.task_center.dispatcher import _dispatch_group_bot_required_channel_follow, recover_pending_visibility_credits
from app.services.task_center.group_bot_observation import restart_admission_observation
from app.services.task_center.payloads import GROUP_BOT_CHANNEL_FOLLOW_ACTION_TYPE, GroupBotRequiredChannelFollowPayload

pytestmark = pytest.mark.no_postgres


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return Session(engine)


def test_plan_required_channel_follow_actions_writes_admission_bound_fields():
    with _session() as session:
        session.add(Tenant(id=1, name="t"))
        task = Task(
            id="task-ai-1",
            tenant_id=1,
            name="ai",
            type="group_ai_chat",
            status="running",
            type_config={"target_group_id": 7, "hourly_min_messages": 30},
        )
        session.add(task)
        session.flush()
        admission = ensure_admission_after_join(
            session,
            tenant_id=1,
            group_id=7,
            account_id=11,
            membership_action_id="join-1",
            join_start_cursor="100",
        )
        ingest_trusted_bot_prompt(
            session,
            admission=admission,
            message_id="bot-1",
            text="请关注 @school_news",
            bot_peer_id="900",
            is_admin_bot=True,
            control_buttons=[
                {"row": 0, "col": 0, "text": "关注频道", "url": "https://t.me/school_news", "action_type": "url"}
            ],
            bound_task_id=task.id,
        )
        actions = list(
            session.scalars(
                select(Action).where(
                    Action.task_id == task.id,
                    Action.action_type == GROUP_BOT_CHANNEL_FOLLOW_ACTION_TYPE,
                )
            )
        )
        assert len(actions) == 1
        payload = actions[0].payload or {}
        assert payload["admission_bound_task_id"] == task.id
        assert int(payload["admission_bound_account_id"]) == 11
        assert payload["channel_ref"] == "school_news"
        assert payload["source_channel_url"] == "https://t.me/school_news"
        follow = session.scalar(select(GroupBotRequiredChannelFollow).where(GroupBotRequiredChannelFollow.admission_id == admission.id))
        assert follow is not None
        assert follow.action_id == actions[0].id


def test_dispatch_group_bot_required_channel_follow_marks_admission():
    with _session() as session:
        session.add(Tenant(id=1, name="t"))
        account = TgAccount(id=11, tenant_id=1, display_name="a", username="user11", phone_masked="+1")
        task = Task(
            id="task-ai-1",
            tenant_id=1,
            name="ai",
            type="group_ai_chat",
            status="running",
            type_config={"target_group_id": 7, "hourly_min_messages": 30},
        )
        session.add_all([account, task])
        session.flush()
        admission = ensure_admission_after_join(
            session,
            tenant_id=1,
            group_id=7,
            account_id=11,
            membership_action_id="join-1",
            join_start_cursor="100",
        )
        ingest_trusted_bot_prompt(
            session,
            admission=admission,
            message_id="bot-1",
            text="请关注 @school_news",
            bot_peer_id="900",
            is_admin_bot=True,
            control_buttons=[
                {"row": 0, "col": 0, "text": "关注频道", "url": "https://t.me/school_news", "action_type": "url"}
            ],
            bound_task_id=task.id,
        )
        action = session.scalar(
            select(Action).where(Action.action_type == GROUP_BOT_CHANNEL_FOLLOW_ACTION_TYPE)
        )
        assert action is not None
        payload = GroupBotRequiredChannelFollowPayload.model_validate(action.payload)
        ok = _dispatch_group_bot_required_channel_follow(session, action, account, credentials=None, payload=payload)
        assert ok is True
        session.refresh(admission)
        assert admission.state in {"awaiting_group_bot_confirmation", "group_bot_admission_ready"}
        follow = session.scalar(select(GroupBotRequiredChannelFollow))
        assert follow is not None
        assert follow.status == "success"


def test_text_only_channel_reference_does_not_mutate_admission():
    with _session() as session:
        task = Task(
            id="task-ai-1",
            tenant_id=1,
            name="ai",
            type="group_ai_chat",
            status="running",
            type_config={"target_group_id": 7},
        )
        session.add_all([Tenant(id=1, name="t"), task])
        admission = ensure_admission_after_join(
            session,
            tenant_id=1,
            group_id=7,
            account_id=11,
            membership_action_id="join-1",
            join_start_cursor="100",
        )

        ingest_trusted_bot_prompt(
            session,
            admission=admission,
            message_id="bot-1",
            text="请关注 @school_news",
            bot_peer_id="900",
            is_admin_bot=True,
            bound_task_id=task.id,
        )

        assert admission.state == "awaiting_group_bot_rule"
        assert admission.required_channel_refs == []
        assert session.scalar(select(Action).where(Action.action_type == GROUP_BOT_CHANNEL_FOLLOW_ACTION_TYPE)) is None
        assert session.scalar(select(GroupBotRequiredChannelFollow)) is None


def test_new_prompt_rearms_paused_follow_only_after_admission_restart():
    with _session() as session:
        group = TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="g", group_type="supergroup")
        account = TgAccount(id=11, tenant_id=1, display_name="a", phone_masked="+1")
        task = Task(id="task-ai-1", tenant_id=1, name="ai", type="group_ai_chat", status="running")
        session.add_all([Tenant(id=1, name="t"), group, account, task])
        admission = ensure_admission_after_join(
            session,
            tenant_id=1,
            group_id=group.id,
            account_id=account.id,
            membership_action_id="join-1",
            join_start_cursor="100",
        )
        controls = [{"row": 0, "col": 0, "text": "关注频道", "url": "https://t.me/school_news", "action_type": "url"}]
        ingest_trusted_bot_prompt(
            session,
            admission=admission,
            message_id="control-old",
            text="请关注 https://t.me/school_news 后完成验证",
            bot_peer_id="900",
            is_admin_bot=True,
            control_buttons=controls,
            bound_task_id=task.id,
        )
        old_action = session.scalar(select(Action).where(Action.action_type == GROUP_BOT_CHANNEL_FOLLOW_ACTION_TYPE))
        follow = session.scalar(select(GroupBotRequiredChannelFollow))
        assert old_action is not None and follow is not None
        old_action.status = "skipped"
        follow.status = "blocked"
        follow.failure_code = "group_bot_control_prompt_unverified"
        admission.state = "group_bot_policy_unresolved"
        session.add(
            GroupContextMessage(
                tenant_id=1,
                group_id=group.id,
                listener_account_id=account.id,
                remote_message_id="200",
                content="current waterline",
            )
        )
        restart_admission_observation(
            session,
            admission=admission,
            expected_admission_version=1,
            reason="controlled pilot restart",
            evidence_ref="incident:prompt-unverified",
        )

        ingest_trusted_bot_prompt(
            session,
            admission=admission,
            message_id="control-new",
            text="请关注 https://t.me/school_news 后完成验证",
            bot_peer_id="900",
            is_admin_bot=True,
            control_buttons=controls,
            bound_task_id=task.id,
        )

        actions = list(session.scalars(select(Action).where(Action.action_type == GROUP_BOT_CHANNEL_FOLLOW_ACTION_TYPE)))
        assert len(actions) == 2
        assert follow.status == "pending"
        assert follow.failure_code == ""
        assert follow.source_message_id == "control-new"
        assert follow.action_id != old_action.id
        assert actions[-1].payload["admission_version"] == 2
        assert actions[-1].payload["source_message_id"] == "control-new"


def test_current_prompt_follow_completion_ignores_rejected_previous_prompt_rows():
    with _session() as session:
        session.add(Tenant(id=1, name="t"))
        admission = ensure_admission_after_join(
            session,
            tenant_id=1,
            group_id=7,
            account_id=11,
            membership_action_id="join-1",
            join_start_cursor="100",
        )
        admission.required_channel_refs = ["current_channel"]
        session.add_all(
            [
                GroupBotRequiredChannelFollow(
                    admission_id=admission.id,
                    channel_ref="promotion_channel",
                    status="blocked",
                    failure_code="group_bot_control_prompt_unverified",
                ),
                GroupBotRequiredChannelFollow(
                    admission_id=admission.id,
                    channel_ref="current_channel",
                    status="pending",
                ),
            ]
        )
        session.flush()

        mark_channel_follow_completed(session, admission=admission, channel_ref="current_channel")

        assert admission.state == "awaiting_group_bot_confirmation"


def test_probe_message_visible_mock_paths():
    from app.integrations.telegram.mock import TelegramGateway

    gw = TelegramGateway()
    visible = gw.probe_message_visible(1, "-1001", 10)
    assert visible.ok is True
    assert visible.visible is True
    missing = gw.probe_message_visible(1, "missing-peer", 10)
    assert missing.visible is False

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.integrations.telegram import ChannelMembershipResult, OperationResult
from app.models import (
    AccountGroupAdmissionFact,
    Action,
    ExecutionAttempt,
    GatewayRequestEvidenceJournal,
    GroupBotAdmission,
    GroupContextMessage,
    Task,
    TaskGroupBotAdmission,
    Tenant,
    TgAccount,
    TgGroup,
)
from app.services._common import _now
from app.services.task_center.group_bot_requirement_recovery import (
    replan_group_bot_requirement_action,
)
from app.services.task_center.group_bot_task_restart import rearm_stopped_admission_actions
from app.services.task_center import dispatcher
from app.services.task_center.payloads import (
    GroupBotConfirmationButtonPayload,
    GroupBotRequiredChannelFollowPayload,
)
from app.services.task_center.task_group_bot_admission_prompts import record_control_facts
from app.services.task_center.task_group_bot_admission_v2 import evaluate_task_admission
from app.services.task_center.task_prejoin_channels import ensure_prejoin_channels


pytestmark = pytest.mark.no_postgres


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return Session(engine)


def _task() -> Task:
    return Task(
        id="task-fact-first",
        tenant_id=1,
        name="AI 群",
        type="group_ai_chat",
        status="running",
        fulfillment_contract_version="fact_first_v3",
        type_config={"target_group_id": 7, "group_bot_admission_required": True},
    )


def _admission(task: Task) -> TaskGroupBotAdmission:
    return TaskGroupBotAdmission(
        tenant_id=1,
        task_id=task.id,
        account_id=11,
        target_group_id=7,
        state="observing",
        no_prompt_pass_at=_now() + timedelta(seconds=30),
        surface_identity_hash="a" * 64,
        surface_identity={"observed_start_cursor": "100"},
    )


def test_fact_first_prompt_materializes_task_actions_without_legacy_rows() -> None:
    with _session() as session:
        task = _task()
        admission = _admission(task)
        session.add_all([
            Tenant(id=1, name="t"),
            task,
            TgAccount(id=11, tenant_id=1, display_name="账号甲", phone_masked="+11", session_ciphertext="s"),
            TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="g"),
            admission,
        ])
        session.flush()
        message = SimpleNamespace(
            remote_message_id="prompt-1",
            sender_peer_id="bot-1",
            sender_role="admin",
            is_bot=True,
            content="账号甲，请先关注频道并完成验证 https://t.me/alpha",
            control_buttons=[
                {"row": 0, "col": 0, "text": "关注频道", "url": "https://t.me/alpha", "action_type": "url"},
                {"row": 1, "col": 0, "text": "完成验证", "action_type": "callback"},
            ],
        )

        assert record_control_facts(session, admission, [message], end_cursor=101) == 1
        actions = list(session.scalars(select(Action).where(Action.task_id == task.id)))
        assert {action.action_type for action in actions} == {
            "group_bot_channel_follow",
            "group_bot_confirmation_button",
        }
        assert all(action.payload["task_group_bot_admission_id"] == admission.id for action in actions)
        assert all(action.payload["admission_id"] is None for action in actions)
        assert session.scalar(select(GroupBotAdmission)) is None
        assert session.scalar(select(AccountGroupAdmissionFact)) is not None


def test_configured_prejoin_is_reused_for_already_joined_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _session() as session:
        task = _task()
        task.group_ai_prejoin_channel_ids = ["zzxshc", "zzxshbg"]
        account = TgAccount(
            id=11,
            tenant_id=1,
            display_name="账号甲",
            phone_masked="+11",
            session_ciphertext="s",
        )
        group = TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="g")
        action = Action(
            id="send-1",
            tenant_id=1,
            task_id=task.id,
            task_type=task.type,
            action_type="send_message",
            account_id=account.id,
            status="pending",
            payload={"group_id": group.id},
        )
        calls: list[str] = []
        monkeypatch.setattr(
            "app.services.task_center.task_prejoin_channels.gateway.ensure_channel_membership",
            lambda account_id, ref, session_ciphertext, credentials, invite_link: (
                calls.append(ref) or OperationResult(True, detail="joined")
            ),
        )
        session.add_all([Tenant(id=1, name="t"), task, account, group, action])
        session.flush()

        assert ensure_prejoin_channels(
            session,
            task=task,
            action=action,
            account=account,
            credentials=object(),
            target_group=group,
        )
        second_action = Action(
            id="send-2",
            tenant_id=1,
            task_id=task.id,
            task_type=task.type,
            action_type="send_message",
            account_id=account.id,
            status="pending",
            payload={"group_id": group.id},
        )
        session.add(second_action)
        session.flush()

        assert ensure_prejoin_channels(
            session,
            task=task,
            action=second_action,
            account=account,
            credentials=object(),
            target_group=group,
        )
        assert set(calls) == {"zzxshc", "zzxshbg"}
        assert len(calls) == 2


def test_fact_first_observation_does_not_import_legacy_ready() -> None:
    with _session() as session:
        task = _task()
        session.add_all([
            Tenant(id=1, name="t"),
            task,
            TgAccount(id=11, tenant_id=1, display_name="账号甲", phone_masked="+11", session_ciphertext="s"),
            TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="g"),
            GroupBotAdmission(
                tenant_id=1,
                group_id=7,
                account_id=11,
                state="group_bot_admission_ready",
                post_send_visibility_state="visible_confirmed",
            ),
        ])
        session.flush()

        decision = evaluate_task_admission(
            session,
            task_id=task.id,
            tenant_id=1,
            group_id=7,
            account_id=11,
        )

        assert decision.allowed is False
        assert decision.code == "c2_observation_started"
        admission = session.get(TaskGroupBotAdmission, decision.admission_id)
        assert admission is not None and admission.state == "observing"


def test_requirement_replan_requires_false_gateway_mutation() -> None:
    with _session() as session:
        task = _task()
        account = TgAccount(id=11, tenant_id=1, display_name="账号甲", phone_masked="+11")
        admission = _admission(task)
        session.add_all([Tenant(id=1, name="t"), task, account, admission])
        session.flush()
        action = Action(
            id="old-follow",
            tenant_id=1,
            task_id=task.id,
            task_type=task.type,
            action_type="group_bot_channel_follow",
            account_id=account.id,
            status="failed",
            payload={
                "group_id": 7,
                "admission_id": None,
                "admission_version": 1,
                "channel_ref": "alpha",
                "source_message_id": "prompt-1",
                "source_channel_url": "https://t.me/alpha",
                "admission_bound_task_id": task.id,
                "admission_bound_account_id": account.id,
                "task_group_bot_admission_id": admission.id,
                "source_fingerprint": "f" * 64,
                "requirement_action_key": "f:alpha",
            },
            result={"error_code": "required_channel_follow_failed"},
        )
        session.add(action)
        session.flush()
        attempt = ExecutionAttempt(
            tenant_id=1,
            action_id=action.id,
            account_id=account.id,
            attempt_no=1,
            status="failed",
            failure_type="FloodWait",
            gateway_call_started_at=_now(),
            after_call_at=_now(),
        )
        session.add(attempt)
        session.flush()
        session.add(GatewayRequestEvidenceJournal(
            tenant_id=1,
            action_id=action.id,
            execution_attempt_id=attempt.id,
            account_id=account.id,
            gateway_request_identity="telegram-gateway:test",
            request_fingerprint="a" * 64,
            target_fingerprint="b" * 64,
            result_fingerprint="c" * 64,
            evidence_hash="d" * 64,
            failure_code="FloodWait",
            remote_mutation_state="false",
        ))
        session.flush()

        replacement = replan_group_bot_requirement_action(session, action)

        assert replacement is not None
        assert replacement.id != action.id
        assert replacement.status == "pending"
        assert replacement.payload["replan_attempt"] == 1
        assert action.status == "skipped"
        assert replan_group_bot_requirement_action(session, action) is None


def test_requirement_unknown_is_not_replanned() -> None:
    with _session() as session:
        task = _task()
        account = TgAccount(id=11, tenant_id=1, display_name="账号甲", phone_masked="+11")
        session.add_all([Tenant(id=1, name="t"), task, account])
        session.flush()
        action = Action(
            id="unknown-follow",
            tenant_id=1,
            task_id=task.id,
            task_type=task.type,
            action_type="group_bot_channel_follow",
            account_id=account.id,
            status="closed_unknown",
            payload={},
            result={"error_code": "required_channel_follow_failed"},
        )
        session.add(action)
        session.flush()
        attempt = ExecutionAttempt(
            tenant_id=1,
            action_id=action.id,
            account_id=account.id,
            attempt_no=1,
            status="result_unknown",
            failure_type="FloodWait",
            gateway_call_started_at=_now(),
            after_call_at=_now(),
        )
        session.add(attempt)
        session.flush()
        session.add(GatewayRequestEvidenceJournal(
            tenant_id=1,
            action_id=action.id,
            execution_attempt_id=attempt.id,
            account_id=account.id,
            gateway_request_identity="telegram-gateway:unknown",
            request_fingerprint="a" * 64,
            target_fingerprint="b" * 64,
            result_fingerprint="c" * 64,
            evidence_hash="d" * 64,
            remote_mutation_state="unknown",
        ))
        session.flush()

        assert replan_group_bot_requirement_action(session, action) is None
        assert action.status == "closed_unknown"


def test_task_scoped_requirement_dispatch_does_not_touch_legacy_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _session() as session:
        task = _task()
        account = TgAccount(
            id=11,
            tenant_id=1,
            display_name="账号甲",
            phone_masked="+11",
            session_ciphertext="s",
        )
        admission = _admission(task)
        session.add_all([
            Tenant(id=1, name="t"),
            task,
            account,
            TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="g"),
            admission,
        ])
        session.flush()
        message = SimpleNamespace(
            remote_message_id="prompt-1",
            sender_peer_id="bot-1",
            sender_role="admin",
            is_bot=True,
            content="账号甲，请先关注 https://t.me/alpha",
            control_buttons=[
                {"row": 0, "col": 0, "text": "关注频道", "url": "https://t.me/alpha", "action_type": "url"},
            ],
        )
        record_control_facts(session, admission, [message], end_cursor=101)
        action = session.scalar(select(Action).where(Action.action_type == "group_bot_channel_follow"))
        assert action is not None
        monkeypatch.setattr(
            dispatcher.gateway,
            "follow_group_bot_required_channel",
            lambda *_args, **_kwargs: ChannelMembershipResult(
                True,
                detail="broadcast:123",
                membership_status="joined",
                remote_mutation_started=True,
            ),
        )
        payload = GroupBotRequiredChannelFollowPayload.model_validate(action.payload)

        assert dispatcher._dispatch_group_bot_required_channel_follow(
            session, action, account, None, payload,
        )
        assert action.status == "success"
        assert action.result["task_group_bot_admission_id"] == admission.id
        assert session.scalar(select(GroupBotAdmission)) is None


def test_task_confirmation_waits_for_follow_then_uses_task_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _session() as session:
        task = _task()
        account = TgAccount(id=11, tenant_id=1, display_name="账号甲", phone_masked="+11", session_ciphertext="s")
        admission = _admission(task)
        session.add_all([
            Tenant(id=1, name="t"),
            task,
            account,
            TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="g"),
            admission,
        ])
        session.flush()
        message = SimpleNamespace(
            remote_message_id="prompt-1",
            sender_peer_id="bot-1",
            sender_role="admin",
            is_bot=True,
            content="账号甲，请完成验证 https://t.me/alpha",
            control_buttons=[
                {"row": 0, "col": 0, "text": "关注频道", "url": "https://t.me/alpha", "action_type": "url"},
                {"row": 1, "col": 0, "text": "完成验证", "action_type": "callback"},
            ],
        )
        record_control_facts(session, admission, [message], end_cursor=101)
        follow = session.scalar(select(Action).where(Action.action_type == "group_bot_channel_follow"))
        confirmation = session.scalar(select(Action).where(Action.action_type == "group_bot_confirmation_button"))
        assert follow is not None and confirmation is not None
        follow.status = "success"
        session.add(GroupContextMessage(
            tenant_id=1,
            group_id=7,
            listener_account_id=11,
            sender_peer_id="bot-1",
            is_bot=True,
            remote_message_id="prompt-1",
            content=message.content,
            control_buttons=message.control_buttons,
        ))
        monkeypatch.setattr(
            dispatcher.gateway,
            "click_group_bot_confirmation_button",
            lambda *_args, **_kwargs: OperationResult(
                True,
                detail="clicked",
                remote_mutation_started=True,
            ),
        )
        payload = GroupBotConfirmationButtonPayload.model_validate(confirmation.payload)

        assert dispatcher._dispatch_group_bot_confirmation_button(
            session, confirmation, account, None, payload,
        )
        assert confirmation.status == "success"
        assert confirmation.result["task_group_bot_admission_id"] == admission.id


def test_fact_first_task_restart_rebuilds_stopped_requirement_action() -> None:
    with _session() as session:
        task = _task()
        account = TgAccount(id=11, tenant_id=1, display_name="账号甲", phone_masked="+11", session_ciphertext="s")
        admission = _admission(task)
        session.add_all([
            Tenant(id=1, name="t"),
            task,
            account,
            TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="g"),
            admission,
        ])
        session.flush()
        message = SimpleNamespace(
            remote_message_id="prompt-1",
            sender_peer_id="bot-1",
            sender_role="admin",
            is_bot=True,
            content="账号甲，请先关注 https://t.me/alpha",
            control_buttons=[
                {"row": 0, "col": 0, "text": "关注频道", "url": "https://t.me/alpha", "action_type": "url"},
            ],
        )
        record_control_facts(session, admission, [message], end_cursor=101)
        old_action = session.scalar(select(Action).where(Action.action_type == "group_bot_channel_follow"))
        assert old_action is not None
        old_action.status = "skipped"
        old_action.result = {"error_code": "task_stopped"}
        session.flush()

        assert rearm_stopped_admission_actions(session, task=task) == 1
        actions = list(session.scalars(select(Action).order_by(Action.created_at, Action.id)))
        assert len(actions) == 2
        assert old_action.status == "skipped"
        assert actions[-1].status == "pending"
        assert actions[-1].payload["replan_attempt"] == 1

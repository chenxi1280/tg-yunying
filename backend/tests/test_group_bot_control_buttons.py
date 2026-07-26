from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.integrations.telegram import OperationResult
from app.models import Action, AccountStatus, GroupBotAdmission, GroupContextMessage, OperationTarget, Task, Tenant, TgAccount, TgGroup
from app.services.group_listener_context_writer import insert_context_snapshots
from app.services.task_center import dispatcher
from app.services.task_center.group_bot_admission import create_policy, ensure_admission_after_join
from app.services.task_center.payloads import EnsureChannelMembershipPayload


pytestmark = pytest.mark.no_postgres


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return Session(engine)


def _account(account_id: int, name: str) -> TgAccount:
    return TgAccount(
        id=account_id,
        tenant_id=1,
        display_name=name,
        phone_masked=f"+100{account_id}",
        status=AccountStatus.ACTIVE.value,
        session_ciphertext=f"session-{account_id}",
    )


def _group() -> TgGroup:
    return TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="群管测试群", group_type="supergroup")


def _bot_snapshot(*, content: str, peer_id: str, sender_role: str = "unknown", buttons: list[dict] | None = None):
    return SimpleNamespace(
        remote_message_id="bot-message-1",
        sender_peer_id=peer_id,
        sender_name="群管机器人",
        sender_username="",
        sender_peer_type="bot",
        content=content,
        message_type="text",
        sent_at=None,
        is_bot=True,
        sender_role=sender_role,
        sender_is_admin=False,
        caption="",
        media_type="",
        media_fingerprint="",
        media_group_id="",
        media_group_index=0,
        media_group_total=1,
        control_buttons=buttons or [],
    )


def _insert_snapshot(session: Session, group: TgGroup, account: TgAccount, snapshot: object) -> None:
    insert_context_snapshots(
        session,
        group,
        account,
        [snapshot],
        ignored_sender=lambda _snapshot: False,
        create_source_media=False,
        learning_scene=None,
    )


def test_untrusted_bot_does_not_mutate_concurrent_waiting_admissions() -> None:
    with _session() as session:
        group = _group()
        first, second = _account(11, "账号甲"), _account(12, "账号乙")
        session.add_all([Tenant(id=1, name="t"), group, first, second])
        for account in (first, second):
            ensure_admission_after_join(
                session,
                tenant_id=1,
                group_id=group.id,
                account_id=account.id,
                membership_action_id=f"join-{account.id}",
                join_start_cursor="100",
            )

        _insert_snapshot(session, group, first, _bot_snapshot(content="请先完成群内验证后发言", peer_id="untrusted-bot"))

        states = list(
            session.scalars(
                select(GroupBotAdmission.state).where(GroupBotAdmission.group_id == group.id).order_by(GroupBotAdmission.account_id)
            )
        )
        assert states == ["awaiting_group_bot_rule", "awaiting_group_bot_rule"]


def test_policy_trusted_button_prompt_persists_controls_and_plans_exact_actions() -> None:
    with _session() as session:
        group, account = _group(), _account(11, "账号甲")
        task = Task(
            id="task-ai-1",
            tenant_id=1,
            name="ai",
            type="group_ai_chat",
            status="running",
            type_config={"target_group_id": group.id},
        )
        session.add_all([Tenant(id=1, name="t"), group, account, task])
        admission = ensure_admission_after_join(
            session,
            tenant_id=1,
            group_id=group.id,
            account_id=account.id,
            membership_action_id="join-11",
            join_start_cursor="100",
        )
        create_policy(
            session,
            tenant_id=1,
            group_id=group.id,
            completion_policy="explicit_bot_confirmation",
            trusted_bot_peer_id="trusted-bot",
            reason="production prompt evidence",
            evidence_ref="message:bot-message-1",
            created_by="operator",
        )
        buttons = [
            {"row": 0, "col": 0, "text": "关注频道", "url": "https://t.me/channel_alpha", "action_type": "url"},
            {"row": 1, "col": 0, "text": "我已加入", "url": "", "action_type": "callback"},
        ]

        _insert_snapshot(
            session,
            group,
            account,
            _bot_snapshot(content="账号甲，请先完成频道关注", peer_id="trusted-bot", buttons=buttons),
        )

        controls = session.scalar(select(GroupContextMessage).where(GroupContextMessage.remote_message_id == "bot-message-1"))
        actions = list(session.scalars(select(Action).where(Action.task_id == task.id).order_by(Action.action_type)))
        assert controls is not None
        assert controls.control_buttons == buttons
        assert admission.required_channel_refs == ["channel_alpha"]
        assert [action.action_type for action in actions] == [
            "group_bot_confirmation_button",
            "group_bot_required_channel_follow",
        ]


def test_confirmation_action_calls_exact_gateway_operation_without_marking_ready(monkeypatch) -> None:
    with _session() as session:
        group, account = _group(), _account(11, "账号甲")
        session.add_all([Tenant(id=1, name="t"), group, account])
        admission = ensure_admission_after_join(
            session,
            tenant_id=1,
            group_id=group.id,
            account_id=account.id,
            membership_action_id="join-11",
            join_start_cursor="100",
        )
        admission.state = "awaiting_group_bot_confirmation"
        admission.trusted_bot_peer_id = "trusted-bot"
        action = Action(
            id="confirm-1",
            tenant_id=1,
            task_type="group_ai_chat",
            action_type="group_bot_confirmation_button",
            account_id=account.id,
            status="executing",
            payload={},
        )
        session.add(action)
        payload = SimpleNamespace(
            group_id=group.id,
            admission_id=admission.id,
            admission_version=admission.admission_version,
            source_message_id="bot-message-1",
            trusted_bot_peer_id="trusted-bot",
            button_row=1,
            button_col=0,
            button_text="我已加入",
            button_type="callback",
        )
        calls: list[tuple] = []

        def click_confirmation(account_id, group_peer_id, source_message_id, trusted_bot_peer_id, row, col, text, session_ciphertext, credentials):
            calls.append((account_id, group_peer_id, source_message_id, trusted_bot_peer_id, row, col, text))
            return OperationResult(True, detail="callback clicked")

        monkeypatch.setattr(dispatcher.gateway, "click_group_bot_confirmation_button", click_confirmation, raising=False)
        context = dispatcher.ActionDispatchContext(account, payload, None, None)

        assert dispatcher._dispatch_credentialed_action(session, action, context, credentials=object()) is True
        assert calls == [(11, "-1007", "bot-message-1", "trusted-bot", 1, 0, "我已加入")]
        assert action.status == "success"
        assert admission.state == "awaiting_group_bot_confirmation"


def test_second_group_membership_defers_before_gateway_while_admission_is_open(monkeypatch) -> None:
    with _session() as session:
        group, account = _group(), _account(11, "账号甲")
        task = Task(
            id="task-ai-1",
            tenant_id=1,
            name="ai",
            type="group_ai_chat",
            status="running",
            type_config={"group_bot_admission_required": True, "target_group_id": group.id},
        )
        target = OperationTarget(id=8, tenant_id=1, target_type="group", tg_peer_id="-1007", title=group.title)
        session.add_all([Tenant(id=1, name="t"), group, account, task, target])
        ensure_admission_after_join(
            session,
            tenant_id=1,
            group_id=group.id,
            account_id=99,
            membership_action_id="join-existing",
            join_start_cursor="100",
        )
        payload = EnsureChannelMembershipPayload(
            channel_id="-1007",
            channel_target_id=target.id,
            target_type="group",
            target_display=group.title,
            require_send=True,
        )
        action = Action(
            id="join-second",
            tenant_id=1,
            task_id=task.id,
            task_type="group_ai_chat",
            action_type="ensure_target_membership",
            account_id=account.id,
            status="executing",
            payload=payload.model_dump(),
        )
        session.add(action)
        gateway_calls: list[object] = []
        monkeypatch.setattr(
            dispatcher.gateway,
            "ensure_channel_membership",
            lambda *args, **kwargs: gateway_calls.append((args, kwargs)) or OperationResult(True, detail="joined"),
        )

        assert dispatcher._dispatch_channel_membership(session, action, account, object(), payload) is True
        assert gateway_calls == []
        assert action.status == "pending"
        assert action.result["error_code"] == "group_bot_admission_window_busy"

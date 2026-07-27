from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.integrations.telegram import GroupControlButtonSnapshot, GroupMessageSnapshot, OperationResult
from app.models import Action, AccountStatus, GroupBotAdmission, GroupBotRequiredChannelFollow, GroupContextMessage, OperationTarget, Task, Tenant, TgAccount, TgGroup
from app.services.group_listener_context_writer import insert_context_snapshots
from app.services.task_center import dispatcher
from app.services.task_center.group_bot_admission import create_policy, ensure_admission_after_join
from app.services.task_center.payloads import (
    GROUP_BOT_CHANNEL_FOLLOW_ACTION_TYPE,
    EnsureChannelMembershipPayload,
    GroupBotConfirmationButtonPayload,
)


pytestmark = pytest.mark.no_postgres


@pytest.fixture(autouse=True)
def _default_exact_confirmation_source(monkeypatch) -> None:
    monkeypatch.setattr(
        dispatcher.gateway,
        "fetch_group_message",
        lambda *_args, **_kwargs: None,
        raising=False,
    )


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


def _serialized_membership_setup(session: Session):
    group, first, second = _group(), _account(11, "账号甲"), _account(12, "账号乙")
    task = Task(
        id="task-ai-1",
        tenant_id=1,
        name="ai",
        type="group_ai_chat",
        status="running",
        type_config={"group_bot_admission_required": True, "target_group_id": group.id},
    )
    target = OperationTarget(id=8, tenant_id=1, target_type="group", tg_peer_id=group.tg_peer_id, title=group.title)
    session.add_all([Tenant(id=1, name="t"), group, first, second, task, target])
    first_admission = ensure_admission_after_join(
        session,
        tenant_id=1,
        group_id=group.id,
        account_id=first.id,
        membership_action_id="join-11",
        join_start_cursor="100",
    )
    payload = EnsureChannelMembershipPayload(
        channel_id=group.tg_peer_id,
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
        account_id=second.id,
        status="executing",
        payload=payload.model_dump(),
    )
    session.add(action)
    return first_admission, action, payload, second


def test_group_bot_action_types_fit_action_storage_limit() -> None:
    storage_limit = Action.__table__.c.action_type.type.length

    assert GROUP_BOT_CHANNEL_FOLLOW_ACTION_TYPE == "group_bot_channel_follow"
    assert len(GROUP_BOT_CHANNEL_FOLLOW_ACTION_TYPE) <= storage_limit
    assert len("group_bot_confirmation_button") <= storage_limit


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


def test_policy_trusted_replayed_button_prompt_updates_legacy_context_and_plans_exact_actions() -> None:
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
        session.add(
            GroupContextMessage(
                tenant_id=1,
                group_id=group.id,
                listener_account_id=account.id,
                sender_peer_id="trusted-bot",
                sender_name="群管机器人",
                is_bot=True,
                sender_role="unknown",
                content="账号甲，请先完成频道关注",
                remote_message_id="bot-message-1",
                control_buttons=[],
            )
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
            GROUP_BOT_CHANNEL_FOLLOW_ACTION_TYPE,
            "group_bot_confirmation_button",
        ]


def test_policy_trusted_promotion_without_control_signal_does_not_mutate_admission() -> None:
    with _session() as session:
        group, account = _group(), _account(11, "账号甲")
        task = Task(id="task-ai-1", tenant_id=1, name="ai", type="group_ai_chat", status="running", type_config={"target_group_id": group.id})
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
            evidence_ref="message:control-1",
            created_by="operator",
        )
        _insert_snapshot(
            session,
            group,
            account,
            _bot_snapshot(
                content="#推广 联系方式: @promo_contact",
                peer_id="trusted-bot",
                buttons=[
                    {"row": 0, "col": 0, "text": "推广相册", "url": "https://t.me/promo_album", "action_type": "url"},
                    {"row": 0, "col": 1, "text": "推广直连", "url": "https://t.me/promo_contact", "action_type": "url"},
                ],
            ),
        )

        assert session.scalar(select(GroupContextMessage)) is not None
        assert admission.state == "awaiting_group_bot_rule"
        assert admission.trusted_bot_peer_id == ""
        assert admission.source_message_id == ""
        assert admission.required_channel_refs == []
        assert session.scalar(select(Action).where(Action.task_id == task.id)) is None
        assert session.scalar(select(GroupBotRequiredChannelFollow)) is None


def test_trusted_foreign_recipient_control_does_not_use_unique_waiting_fallback() -> None:
    with _session() as session:
        group, account = _group(), _account(11, "橘白")
        task = Task(id="task-ai-1", tenant_id=1, name="ai", type="group_ai_chat", status="running", type_config={"target_group_id": group.id})
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
            evidence_ref="message:control-1",
            created_by="operator",
        )
        _insert_snapshot(
            session,
            group,
            account,
            _bot_snapshot(
                content="🐨霏霏吖.😌 Ray，您需要关注我们的频道才能发言。",
                peer_id="trusted-bot",
                buttons=[
                    {"row": 0, "col": 0, "text": "频道", "url": "https://t.me/channel_alpha", "action_type": "url"},
                    {"row": 1, "col": 0, "text": "我已加入", "url": "", "action_type": "callback"},
                ],
            ),
        )

        assert session.scalar(select(GroupContextMessage)) is not None
        assert admission.state == "awaiting_group_bot_rule"
        assert admission.required_channel_refs == []
        assert session.scalar(select(Action).where(Action.task_id == task.id)) is None


def _confirmation_action_fixture(session: Session, *, action_id: str, source_message_id: str):
    group, account = _group(), _account(11, "账号甲")
    task = Task(id="task-ai-1", tenant_id=1, name="ai", type="group_ai_chat", status="running")
    session.add_all([Tenant(id=1, name="t"), group, account, task])
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
    admission.source_message_id = source_message_id
    admission.required_channel_refs = ["channel_alpha"]
    payload = GroupBotConfirmationButtonPayload(
        group_id=group.id,
        admission_id=admission.id,
        admission_version=admission.admission_version,
        source_message_id=source_message_id,
        trusted_bot_peer_id="trusted-bot",
        button_row=1,
        button_col=0,
        button_text="我已加入",
        admission_bound_task_id=task.id,
        admission_bound_account_id=account.id,
    )
    action = Action(
        id=action_id,
        tenant_id=1,
        task_id=task.id,
        task_type="group_ai_chat",
        action_type="group_bot_confirmation_button",
        account_id=account.id,
        status="executing",
        payload=payload.model_dump(),
    )
    session.add(action)
    return group, account, admission, payload, action


def test_confirmation_action_calls_exact_gateway_operation_without_marking_ready(monkeypatch) -> None:
    with _session() as session:
        group, account, admission, payload, action = _confirmation_action_fixture(
            session,
            action_id="confirm-1",
            source_message_id="bot-message-1",
        )
        calls: list[tuple] = []

        monkeypatch.setattr(
            dispatcher.gateway,
            "fetch_group_messages",
            lambda *_args, **_kwargs: [_live_confirmation_snapshot("bot-message-1")],
            raising=False,
        )

        def click_confirmation(account_id, group_peer_id, source_message_id, trusted_bot_peer_id, row, col, text, session_ciphertext, credentials):
            calls.append((account_id, group_peer_id, source_message_id, trusted_bot_peer_id, row, col, text))
            return OperationResult(True, detail="callback clicked")

        monkeypatch.setattr(dispatcher.gateway, "click_group_bot_confirmation_button", click_confirmation, raising=False)
        context = dispatcher.ActionDispatchContext(account, payload, None, None)

        assert dispatcher._dispatch_credentialed_action(session, action, context, credentials=object()) is True
        assert calls == [(11, "-1007", "bot-message-1", "trusted-bot", 1, 0, "我已加入")]
        assert action.status == "success"
        assert admission.state == "awaiting_group_bot_confirmation"


def test_confirmation_action_rebinds_to_live_trusted_button_before_gateway(monkeypatch) -> None:
    with _session() as session:
        group, account, admission, payload, action = _confirmation_action_fixture(
            session,
            action_id="confirm-live-source",
            source_message_id="stale-message",
        )
        calls: list[str] = []
        monkeypatch.setattr(
            dispatcher.gateway,
            "fetch_group_message",
            lambda *_args, **_kwargs: _live_confirmation_snapshot("stale-message"),
            raising=False,
        )
        monkeypatch.setattr(
            dispatcher.gateway,
            "fetch_group_messages",
            lambda *_args, **_kwargs: [_live_confirmation_snapshot("fresh-message", content="账号甲，您需要关注频道后发言")],
            raising=False,
        )
        monkeypatch.setattr(
            dispatcher.gateway,
            "click_group_bot_confirmation_button",
            lambda _account_id, _peer_id, source_message_id, *_args: calls.append(source_message_id) or OperationResult(True),
            raising=False,
        )

        context = dispatcher.ActionDispatchContext(account, payload, None, None)

        assert dispatcher._dispatch_credentialed_action(session, action, context, credentials=object()) is True
        assert calls == ["fresh-message"]
        assert action.payload["source_message_id"] == "fresh-message"
        assert admission.source_message_id == "fresh-message"
        assert action.result["group_bot_confirmation_source_refresh"]["from"] == "stale-message"
        assert session.scalar(select(GroupContextMessage.remote_message_id).where(GroupContextMessage.remote_message_id == "fresh-message")) == "fresh-message"


def test_confirmation_action_uses_exact_source_when_current_window_is_truncated(monkeypatch) -> None:
    with _session() as session:
        group, account, _admission, payload, action = _confirmation_action_fixture(
            session,
            action_id="confirm-exact-source",
            source_message_id="exact-message",
        )
        calls: list[str] = []
        monkeypatch.setattr(
            dispatcher.gateway,
            "fetch_group_message",
            lambda *_args, **_kwargs: _live_confirmation_snapshot("exact-message"),
            raising=False,
        )
        monkeypatch.setattr(
            dispatcher.gateway,
            "fetch_group_messages",
            lambda *_args, **_kwargs: [_non_control_snapshot()],
            raising=False,
        )
        monkeypatch.setattr(
            dispatcher.gateway,
            "click_group_bot_confirmation_button",
            lambda _account_id, _peer_id, source_message_id, *_args: calls.append(source_message_id) or OperationResult(True),
            raising=False,
        )

        context = dispatcher.ActionDispatchContext(account, payload, None, None)

        assert dispatcher._dispatch_credentialed_action(session, action, context, credentials=object()) is True
        assert calls == ["exact-message"]
        assert action.status == "success"
        assert action.result["group_bot_confirmation_live_source"]["lookup"] == "exact_source"


def test_confirmation_action_defers_when_live_source_is_unavailable(monkeypatch) -> None:
    with _session() as session:
        group, account, _admission, payload, action = _confirmation_action_fixture(
            session,
            action_id="confirm-no-live-source",
            source_message_id="stale-message",
        )
        monkeypatch.setattr(
            dispatcher.gateway,
            "fetch_group_messages",
            lambda *_args, **_kwargs: [],
            raising=False,
        )
        monkeypatch.setattr(
            dispatcher.gateway,
            "click_group_bot_confirmation_button",
            lambda *_args: pytest.fail("missing live source must not call Telegram callback"),
            raising=False,
        )

        context = dispatcher.ActionDispatchContext(account, payload, None, None)

        assert dispatcher._dispatch_credentialed_action(session, action, context, credentials=object()) is True
        assert action.status == "pending"
        assert action.result["error_code"] == "group_bot_confirmation_source_stale"


def test_confirmation_action_defers_when_live_source_fetch_fails(monkeypatch) -> None:
    with _session() as session:
        _group_value, account, _admission, payload, action = _confirmation_action_fixture(
            session,
            action_id="confirm-live-fetch-failed",
            source_message_id="stale-message",
        )

        def unavailable(*_args, **_kwargs):
            raise RuntimeError("telegram unavailable")

        monkeypatch.setattr(
            dispatcher.gateway,
            "fetch_group_messages",
            unavailable,
            raising=False,
        )
        monkeypatch.setattr(
            dispatcher.gateway,
            "click_group_bot_confirmation_button",
            lambda *_args: pytest.fail("failed live fetch must not call Telegram callback"),
            raising=False,
        )

        context = dispatcher.ActionDispatchContext(account, payload, None, None)

        assert dispatcher._dispatch_credentialed_action(session, action, context, credentials=object()) is True
        assert action.status == "pending"
        assert action.result["error_code"] == "group_bot_confirmation_live_fetch_failed"


def test_confirmation_action_retries_when_telegram_button_changes_after_live_fetch(monkeypatch) -> None:
    with _session() as session:
        group, account, _admission, payload, action = _confirmation_action_fixture(
            session,
            action_id="confirm-remote-race",
            source_message_id="fresh-message",
        )
        monkeypatch.setattr(
            dispatcher.gateway,
            "fetch_group_messages",
            lambda *_args, **_kwargs: [_live_confirmation_snapshot("fresh-message")],
            raising=False,
        )
        monkeypatch.setattr(
            dispatcher.gateway,
            "click_group_bot_confirmation_button",
            lambda *_args: OperationResult(False, "失败", "peer_invalid", "group_bot_confirmation_button_mismatch"),
            raising=False,
        )

        context = dispatcher.ActionDispatchContext(account, payload, None, None)

        assert dispatcher._dispatch_credentialed_action(session, action, context, credentials=object()) is True
        assert action.status == "pending"
        assert action.result["error_code"] == "group_bot_confirmation_source_stale"
        assert action.result["validation_stage"] == "group_bot_confirmation_live"


def _live_confirmation_snapshot(message_id: str, *, content: str = "请先关注频道后发言") -> GroupMessageSnapshot:
    return GroupMessageSnapshot(
        remote_message_id=message_id,
        sender_peer_id="trusted-bot",
        sender_name="群管机器人",
        content=content,
        is_bot=True,
        control_buttons=(
            GroupControlButtonSnapshot(0, 0, "频道", "https://t.me/channel_alpha", "url"),
            GroupControlButtonSnapshot(1, 0, "我已加入", "", "callback"),
        ),
    )


def _non_control_snapshot() -> GroupMessageSnapshot:
    return GroupMessageSnapshot(
        remote_message_id="noise-message",
        sender_peer_id="ordinary-member",
        sender_name="普通成员",
        content="群里消息很多",
        is_bot=False,
    )


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


def test_deferred_group_membership_runs_after_previous_admission_is_ready(monkeypatch) -> None:
    with _session() as session:
        first_admission, action, payload, second = _serialized_membership_setup(session)
        calls: list[int] = []
        monkeypatch.setattr(
            dispatcher.gateway,
            "ensure_channel_membership",
            lambda *args, **kwargs: calls.append(1) or OperationResult(True, detail="joined"),
        )
        monkeypatch.setattr(
            dispatcher.gateway,
            "probe_target_capabilities",
            lambda *args, **kwargs: OperationResult(True, detail="can_send"),
        )

        assert dispatcher._dispatch_channel_membership(session, action, second, object(), payload) is True
        assert calls == []
        first_admission.state = "group_bot_admission_ready"
        action.status = "executing"

        assert dispatcher._dispatch_channel_membership(session, action, second, object(), payload) is True
        assert calls == [1]
        assert action.status == "success"

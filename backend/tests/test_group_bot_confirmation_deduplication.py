from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.integrations.telegram import GroupControlButtonSnapshot, GroupMessageSnapshot, OperationResult
from app.models import AccountStatus, Action, GroupBotAdmission, Task, Tenant, TgAccount, TgGroup
from app.services.task_center import dispatcher
from app.services.task_center.group_bot_admission import (
    confirmation_action_can_dispatch,
    ensure_admission_after_join,
    plan_confirmation_button_action,
)
from app.services.task_center.payloads import GroupBotConfirmationButtonPayload


pytestmark = pytest.mark.no_postgres


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return Session(engine)


def _duplicate_confirmation_setup(session: Session, *, first_status: str = "success"):
    group = TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="群管测试群", group_type="supergroup")
    account = TgAccount(
        id=11,
        tenant_id=1,
        display_name="账号甲",
        phone_masked="+10011",
        status=AccountStatus.ACTIVE.value,
        session_ciphertext="session-11",
    )
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
    payload = GroupBotConfirmationButtonPayload(
        group_id=group.id,
        admission_id=admission.id,
        admission_version=admission.admission_version,
        source_message_id="bot-message-1",
        trusted_bot_peer_id="trusted-bot",
        button_row=1,
        button_col=0,
        button_text="我已加入",
        admission_bound_task_id=task.id,
        admission_bound_account_id=account.id,
    )
    session.add_all(_confirmation_actions(task, account, payload, first_status))
    session.flush()
    return account, payload


def _confirmation_actions(
    task: Task,
    account: TgAccount,
    payload: GroupBotConfirmationButtonPayload,
    first_status: str,
) -> list[Action]:
    action_data = payload.model_dump()
    fields = {
        "tenant_id": task.tenant_id,
        "task_id": task.id,
        "task_type": task.type,
        "action_type": "group_bot_confirmation_button",
        "account_id": account.id,
    }
    return [
        Action(id="confirm-first", status=first_status, payload={**action_data}, **fields),
        Action(id="confirm-duplicate", status="executing", payload={**action_data}, **fields),
    ]


def _refresh_confirmation_setup(session: Session):
    group = TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="群管测试群", group_type="supergroup")
    account = TgAccount(
        id=11,
        tenant_id=1,
        display_name="账号甲",
        phone_masked="+10011",
        status=AccountStatus.ACTIVE.value,
        session_ciphertext="session-11",
    )
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
    admission.source_message_id = "exact-message"
    admission.required_channel_refs = ["channel_alpha"]
    session.flush()
    payload = GroupBotConfirmationButtonPayload(
        group_id=group.id,
        admission_id=admission.id,
        admission_version=admission.admission_version,
        source_message_id="exact-message",
        trusted_bot_peer_id="trusted-bot",
        button_row=1,
        button_col=0,
        button_text="我已加入",
        admission_bound_task_id=task.id,
        admission_bound_account_id=account.id,
    )
    action = Action(
        id="confirm-refresh",
        tenant_id=1,
        task_id=task.id,
        task_type=task.type,
        action_type="group_bot_confirmation_button",
        account_id=account.id,
        status="executing",
        payload=payload.model_dump(),
    )
    session.add(action)
    return group, account, payload, action


def _control_snapshot(message_id: str, content: str) -> GroupMessageSnapshot:
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


@pytest.mark.parametrize(
    ("window_message_id", "window_content"),
    [
        ("other-account-message", "账号乙，您需要关注频道后发言"),
        ("unattributed-message", "请先关注频道后发言"),
    ],
)
def test_confirmation_refresh_keeps_exact_source_for_non_bound_window_prompt(
    monkeypatch,
    window_message_id: str,
    window_content: str,
) -> None:
    with _session() as session:
        _group, account, payload, action = _refresh_confirmation_setup(session)
        calls: list[str] = []
        monkeypatch.setattr(
            dispatcher.gateway,
            "fetch_group_message",
            lambda *_args, **_kwargs: _control_snapshot("exact-message", "账号甲，您需要关注频道后发言"),
            raising=False,
        )
        monkeypatch.setattr(
            dispatcher.gateway,
            "fetch_group_messages",
            lambda *_args, **_kwargs: [_control_snapshot(window_message_id, window_content)],
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
        assert action.payload["source_message_id"] == "exact-message"
        assert action.result["group_bot_confirmation_live_source"]["lookup"] == "exact_source"


def test_confirmation_action_skips_successful_legacy_duplicate_before_gateway(monkeypatch) -> None:
    with _session() as session:
        account, payload = _duplicate_confirmation_setup(session)
        calls: list[object] = []
        monkeypatch.setattr(
            dispatcher.gateway,
            "click_group_bot_confirmation_button",
            lambda *args: calls.append(args) or OperationResult(True),
            raising=False,
        )

        duplicate = session.get(Action, "confirm-duplicate")
        context = dispatcher.ActionDispatchContext(account, payload, None, None)
        assert dispatcher._dispatch_credentialed_action(session, duplicate, context, credentials=object()) is True
        assert calls == []
        assert duplicate.status == "skipped"
        assert duplicate.result["error_code"] == "group_bot_confirmation_superseded"


def test_confirmation_action_rejects_later_open_duplicate() -> None:
    with _session() as session:
        _, payload = _duplicate_confirmation_setup(session, first_status="pending")
        duplicate = session.get(Action, "confirm-duplicate")
        assert duplicate is not None
        assert not confirmation_action_can_dispatch(
            session,
            action=duplicate,
            admission_id=payload.admission_id,
            admission_version=payload.admission_version,
        )


def test_claim_skips_legacy_duplicate_before_global_account_policy(monkeypatch) -> None:
    with _session() as session:
        _, _ = _duplicate_confirmation_setup(session, first_status="pending")
        duplicate = session.get(Action, "confirm-duplicate")
        assert duplicate is not None
        duplicate.status = "claiming"
        duplicate.claim_owner = "dispatcher-1"
        duplicate.claim_token = "claim-token"
        session.commit()

        monkeypatch.setattr(
            dispatcher,
            "_apply_claim_account_policy",
            lambda *_args: pytest.fail("duplicate must be skipped before account policy"),
        )
        batch = dispatcher.ActionClaimBatch(
            action_ids=(duplicate.id,),
            owner="dispatcher-1",
            token="claim-token",
            reservation_bindings={},
        )

        assert not dispatcher._confirm_action_claim_candidate(session, duplicate.id, batch)
        assert duplicate.status == "skipped"
        assert duplicate.result["error_code"] == "group_bot_confirmation_superseded"


def test_planner_replaces_pending_callback_with_current_admission_source() -> None:
    with _session() as session:
        _, payload = _duplicate_confirmation_setup(session, first_status="pending")
        admission = session.get(GroupBotAdmission, payload.admission_id)
        first = session.get(Action, "confirm-first")
        duplicate = session.get(Action, "confirm-duplicate")
        assert admission is not None and first is not None and duplicate is not None
        admission.source_message_id = "bot-message-2"
        duplicate.status = "skipped"
        session.flush()

        replacement = plan_confirmation_button_action(
            session,
            admission=admission,
            task_id="task-ai-1",
            source_message_id="bot-message-2",
            control_buttons=(
                {
                    "row": 1,
                    "col": 0,
                    "text": "我已加入",
                    "action_type": "callback",
                },
            ),
        )

        assert replacement is not None
        assert first.status == "skipped"
        assert first.result["error_code"] == "group_bot_confirmation_superseded"
        assert replacement.payload["source_message_id"] == "bot-message-2"


def test_claim_skips_stale_confirmation_source_before_global_account_policy(monkeypatch) -> None:
    with _session() as session:
        _, payload = _duplicate_confirmation_setup(session, first_status="skipped")
        admission = session.get(GroupBotAdmission, payload.admission_id)
        duplicate = session.get(Action, "confirm-duplicate")
        assert admission is not None and duplicate is not None
        admission.source_message_id = "bot-message-2"
        duplicate.status = "claiming"
        duplicate.claim_owner = "dispatcher-1"
        duplicate.claim_token = "claim-token"
        session.commit()

        monkeypatch.setattr(
            dispatcher,
            "_apply_claim_account_policy",
            lambda *_args: pytest.fail("stale source must be skipped before account policy"),
        )
        batch = dispatcher.ActionClaimBatch(
            action_ids=(duplicate.id,),
            owner="dispatcher-1",
            token="claim-token",
            reservation_bindings={},
        )

        assert not dispatcher._confirm_action_claim_candidate(session, duplicate.id, batch)
        assert duplicate.status == "skipped"
        assert duplicate.result["error_code"] == "group_bot_confirmation_superseded"

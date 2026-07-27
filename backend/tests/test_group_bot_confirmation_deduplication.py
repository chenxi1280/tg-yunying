from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.integrations.telegram import OperationResult
from app.models import AccountStatus, Action, Task, Tenant, TgAccount, TgGroup
from app.services.task_center import dispatcher
from app.services.task_center.group_bot_admission import confirmation_action_can_dispatch, ensure_admission_after_join
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

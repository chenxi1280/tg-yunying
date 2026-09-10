from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.integrations.telegram.message_observation import GroupMessageObservation
from app.models import Action, Task, TaskGroupBotAdmission, Tenant, TgAccount, TgGroup
from app.services.task_center.task_group_bot_post_send_recovery import (
    POST_SEND_CONTROL_FETCH_LIMIT,
    recover_post_send_interception,
)
from app.services.task_center.task_group_bot_admission_v2 import evaluate_task_admission


pytestmark = pytest.mark.no_postgres


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return Session(engine)


def _seed(session: Session, *, duplicate_name: bool = False):
    task = Task(
        id="task-post-send",
        tenant_id=1,
        name="AI 群",
        type="group_ai_chat",
        status="running",
        fulfillment_contract_version="fact_first_v3",
        type_config={"target_group_id": 7, "group_bot_admission_required": True},
    )
    account = TgAccount(
        id=11,
        tenant_id=1,
        display_name="账号甲",
        username="account_a",
        phone_masked="+11",
        session_ciphertext="cipher",
    )
    admission = TaskGroupBotAdmission(
        id="admission-11",
        tenant_id=1,
        task_id=task.id,
        task_lifecycle_epoch=1,
        account_id=account.id,
        target_group_id=7,
        state="ready",
        no_prompt_pass_at=datetime(2026, 9, 9),
        surface_identity_hash="a" * 64,
        surface_identity={"requirement_bot_peer_id": "bot-1"},
    )
    action = Action(
        id="send-post-send",
        tenant_id=1,
        task_id=task.id,
        task_type="group_ai_chat",
        action_type="send_message",
        account_id=account.id,
        status="unknown_after_send",
        task_lifecycle_epoch=1,
        payload={"group_id": 7, "task_group_bot_admission_id": admission.id},
        result={"telegram_msg_id": "600"},
    )
    session.add_all([
        Tenant(id=1, name="t"),
        task,
        account,
        TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="g"),
        admission,
        action,
    ])
    if duplicate_name:
        session.add_all([
            TgAccount(id=12, tenant_id=1, display_name="账号甲", phone_masked="+12"),
            TaskGroupBotAdmission(
                id="admission-12",
                tenant_id=1,
                task_id=task.id,
                task_lifecycle_epoch=1,
                account_id=12,
                target_group_id=7,
                state="ready",
                no_prompt_pass_at=datetime(2026, 9, 9),
                surface_identity_hash="b" * 64,
                surface_identity={},
            ),
        ])
    session.flush()
    return action, admission


def _prompt(
    *,
    content: str = "7405756184，需要订阅频道才能发言！ https://t.me/alpha",
    viewer_peer_id: str = "7405756184",
    sender_peer_id: str = "bot-1",
    sender_role: str = "admin",
    controls=None,
):
    return SimpleNamespace(
        remote_message_id="601",
        viewer_peer_id=viewer_peer_id,
        sender_peer_id=sender_peer_id,
        sender_role=sender_role,
        is_bot=True,
        content=content,
        control_buttons=controls or [
            {"row": 0, "col": 0, "text": "关注频道", "url": "https://t.me/alpha", "action_type": "url"},
            {"row": 1, "col": 0, "text": "完成验证", "action_type": "callback"},
        ],
    )


def _recover(session: Session, action: Action, fetcher):
    transport = SimpleNamespace(session_ciphertext="cipher", credentials=object())
    def observed_fetcher(*args, **kwargs):
        messages = fetcher(*args, **kwargs)
        if isinstance(messages, GroupMessageObservation):
            return messages
        return GroupMessageObservation(tuple(messages), {"read_status": "test_fixture"})
    return recover_post_send_interception(
        session,
        action,
        target_peer="-1007",
        remote_message_id="600",
        transport=transport,
        fetcher=observed_fetcher,
    )


def test_same_view_admin_prompt_materializes_task_scoped_requirements() -> None:
    with _session() as session:
        action, admission = _seed(session)
        observed = {}

        def fetcher(*args, **kwargs):
            observed.update(kwargs)
            return [_prompt()]

        result = _recover(session, action, fetcher)

        assert result.status == "matched"
        assert admission.state == "requirements_pending"
        assert {item.action_type for item in session.scalars(select(Action).where(
            Action.id != action.id,
        ))} == {"group_bot_channel_follow", "group_bot_confirmation_button"}
        assert observed == {
            "limit": POST_SEND_CONTROL_FETCH_LIMIT,
            "control_only": True,
            "after_message_id": 600,
            "include_diagnostics": True,
        }


def test_ambiguous_display_name_keeps_admission_intercepted() -> None:
    with _session() as session:
        action, admission = _seed(session, duplicate_name=True)

        result = _recover(
            session,
            action,
            lambda *_args, **_kwargs: [_prompt(content="账号甲，请完成验证 https://t.me/alpha")],
        )

        assert result.status == "blocked"
        assert "ambiguous" in result.reason
        assert admission.state == "post_send_intercepted"
        assert list(session.scalars(select(Action).where(Action.id != action.id))) == []


@pytest.mark.parametrize(
    "message,reason",
    [
        (_prompt(sender_peer_id="bot-2"), "bot_mismatch"),
        (_prompt(sender_role="member"), "source_untrusted"),
        (_prompt(content="999999，需要订阅频道才能发言！ https://t.me/alpha"), "unmatched"),
        (_prompt(controls=[{"row": 0, "col": 0, "text": "关注频道", "url": "https://t.me/alpha", "action_type": "url"}]), "callback_missing"),
        (_prompt(content="7405756184，需要订阅频道才能发言！", controls=[{"row": 0, "col": 0, "text": "完成验证", "action_type": "callback"}]), "channel_url_missing"),
    ],
)
def test_incomplete_or_wrong_prompt_does_not_restore(message, reason) -> None:
    with _session() as session:
        action, admission = _seed(session)

        result = _recover(session, action, lambda *_args, **_kwargs: [message])

        assert result.status == "blocked"
        assert reason in result.reason
        assert admission.state == "post_send_intercepted"


def test_fetch_failure_retains_ready_admission_for_hold_retry() -> None:
    with _session() as session:
        action, admission = _seed(session)

        def fail(*_args, **_kwargs):
            raise TimeoutError("offline")

        result = _recover(session, action, fail)

        assert result.status == "retry"
        assert result.reason == "post_send_control_fetch_failed:TimeoutError"
        assert admission.state == "ready"


def test_post_send_intercepted_admission_never_auto_restores_ready() -> None:
    with _session() as session:
        _action, admission = _seed(session)
        admission.state = "post_send_intercepted"
        session.flush()

        decision = evaluate_task_admission(
            session,
            task_id=admission.task_id,
            tenant_id=admission.tenant_id,
            group_id=admission.target_group_id,
            account_id=admission.account_id,
        )

        assert decision.allowed is False
        assert decision.code == "c2_post_send_intercepted"
        assert admission.state == "post_send_intercepted"

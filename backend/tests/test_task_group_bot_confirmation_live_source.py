from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Action, Task, TaskGroupBotAdmission, Tenant, TgAccount, TgGroup
from app.services._common import _now
from app.services.task_center import dispatcher
from app.services.task_center.payloads import GroupBotConfirmationButtonPayload
from app.services.task_center.task_group_bot_admission_prompts import record_control_facts


pytestmark = pytest.mark.no_postgres


def _message(message_id: str, content: str = "账号甲，请完成验证 https://t.me/alpha") -> SimpleNamespace:
    return SimpleNamespace(
        remote_message_id=message_id,
        sender_peer_id="bot-1",
        sender_role="admin",
        is_bot=True,
        content=content,
        control_buttons=[
            {"row": 0, "col": 0, "text": "关注频道", "url": "https://t.me/alpha", "action_type": "url"},
            {"row": 1, "col": 0, "text": "完成验证", "action_type": "callback"},
        ],
    )


def _scenario() -> tuple[Session, TgAccount, TaskGroupBotAdmission, Action]:
    session = Session(create_engine("sqlite:///:memory:", future=True))
    Base.metadata.create_all(session.get_bind())
    task = Task(
        id="task-fact-first-live-source",
        tenant_id=1,
        name="AI 群",
        type="group_ai_chat",
        status="running",
        fulfillment_contract_version="fact_first_v3",
        type_config={"target_group_id": 7, "group_bot_admission_required": True},
    )
    account = TgAccount(id=11, tenant_id=1, display_name="账号甲", phone_masked="+11", session_ciphertext="s")
    admission = TaskGroupBotAdmission(
        tenant_id=1,
        task_id=task.id,
        account_id=account.id,
        target_group_id=7,
        state="observing",
        no_prompt_pass_at=_now() + timedelta(seconds=30),
        surface_identity_hash="a" * 64,
        surface_identity={"observed_start_cursor": "100"},
    )
    session.add_all([Tenant(id=1, name="t"), task, account, TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="g"), admission])
    session.flush()
    record_control_facts(session, admission, [_message("prompt-1")], end_cursor=101)
    follow = session.scalar(select(Action).where(Action.action_type == "group_bot_channel_follow"))
    confirmation = session.scalar(select(Action).where(Action.action_type == "group_bot_confirmation_button"))
    assert follow is not None and confirmation is not None
    follow.status = "success"
    return session, account, admission, confirmation


def test_live_source_supersedes_old_action_and_materializes_replacement(monkeypatch: pytest.MonkeyPatch) -> None:
    session, account, admission, confirmation = _scenario()
    with session:
        new_message = _message("prompt-2", "账号甲，请重新完成验证 https://t.me/alpha")
        monkeypatch.setattr(dispatcher.gateway, "fetch_group_message", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(dispatcher.gateway, "fetch_group_messages", lambda *_args, **_kwargs: [new_message])
        clicked: list[bool] = []
        monkeypatch.setattr(dispatcher.gateway, "click_group_bot_confirmation_button", lambda *_args, **_kwargs: clicked.append(True))

        payload = GroupBotConfirmationButtonPayload.model_validate(confirmation.payload)
        assert dispatcher._dispatch_group_bot_confirmation_button(session, confirmation, account, None, payload)

        replacements = list(session.scalars(select(Action).where(
            Action.action_type == "group_bot_confirmation_button", Action.id != confirmation.id,
        )))
        assert confirmation.status == "skipped"
        assert confirmation.result["error_code"] == "group_bot_confirmation_superseded"
        assert [item.payload["source_message_id"] for item in replacements] == ["prompt-2"]
        assert admission.surface_identity["requirement_source_message_id"] == "prompt-2"
        assert clicked == []


def test_live_fetch_error_defers_without_click(monkeypatch: pytest.MonkeyPatch) -> None:
    session, account, _admission, confirmation = _scenario()
    with session:
        def raise_offline(*_args, **_kwargs):
            raise RuntimeError("offline")

        monkeypatch.setattr(dispatcher.gateway, "fetch_group_message", raise_offline)
        clicked: list[bool] = []
        monkeypatch.setattr(dispatcher.gateway, "click_group_bot_confirmation_button", lambda *_args, **_kwargs: clicked.append(True))

        payload = GroupBotConfirmationButtonPayload.model_validate(confirmation.payload)
        assert dispatcher._dispatch_group_bot_confirmation_button(session, confirmation, account, None, payload)

        assert confirmation.status == "pending"
        assert confirmation.result["error_code"] == "group_bot_confirmation_live_fetch_failed"
        assert clicked == []


def test_same_display_name_defers_as_recipient_ambiguous(monkeypatch: pytest.MonkeyPatch) -> None:
    session, account, admission, confirmation = _scenario()
    with session:
        other = TgAccount(id=12, tenant_id=1, display_name="账号甲", phone_masked="+12", session_ciphertext="s2")
        other_admission = TaskGroupBotAdmission(
            tenant_id=1, task_id=admission.task_id, account_id=other.id, target_group_id=7,
            state="observing", no_prompt_pass_at=_now() + timedelta(seconds=30),
            surface_identity_hash="b" * 64, surface_identity={"observed_start_cursor": "100"},
        )
        session.add_all([other, other_admission])
        session.flush()
        monkeypatch.setattr(dispatcher.gateway, "fetch_group_message", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(dispatcher.gateway, "fetch_group_messages", lambda *_args, **_kwargs: [_message("prompt-2")])
        clicked: list[bool] = []
        monkeypatch.setattr(dispatcher.gateway, "click_group_bot_confirmation_button", lambda *_args, **_kwargs: clicked.append(True))

        payload = GroupBotConfirmationButtonPayload.model_validate(confirmation.payload)
        assert dispatcher._dispatch_group_bot_confirmation_button(session, confirmation, account, None, payload)

        assert confirmation.status == "pending"
        assert confirmation.result["error_code"] == "recipient_ambiguous"
        assert clicked == []


def test_exact_old_source_still_requires_unique_recipient(monkeypatch: pytest.MonkeyPatch) -> None:
    session, account, admission, confirmation = _scenario()
    with session:
        other = TgAccount(id=12, tenant_id=1, display_name="账号甲", phone_masked="+12", session_ciphertext="s2")
        session.add_all([
            other,
            TaskGroupBotAdmission(
                tenant_id=1, task_id=admission.task_id, account_id=other.id, target_group_id=7,
                state="observing", no_prompt_pass_at=_now() + timedelta(seconds=30),
                surface_identity_hash="b" * 64, surface_identity={"observed_start_cursor": "100"},
            ),
        ])
        session.flush()
        monkeypatch.setattr(dispatcher.gateway, "fetch_group_message", lambda *_args, **_kwargs: _message("prompt-1"))
        monkeypatch.setattr(dispatcher.gateway, "fetch_group_messages", lambda *_args, **_kwargs: [])
        clicked: list[bool] = []
        monkeypatch.setattr(dispatcher.gateway, "click_group_bot_confirmation_button", lambda *_args, **_kwargs: clicked.append(True))

        payload = GroupBotConfirmationButtonPayload.model_validate(confirmation.payload)
        assert dispatcher._dispatch_group_bot_confirmation_button(session, confirmation, account, None, payload)

        assert confirmation.status == "pending"
        assert confirmation.result["error_code"] == "recipient_ambiguous"
        assert clicked == []


def test_new_source_with_changed_channels_materializes_new_requirement_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, account, admission, confirmation = _scenario()
    with session:
        changed = _message("prompt-2", "账号甲，请完成验证 https://t.me/beta")
        changed.control_buttons[0]["url"] = "https://t.me/beta"
        monkeypatch.setattr(dispatcher.gateway, "fetch_group_message", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(dispatcher.gateway, "fetch_group_messages", lambda *_args, **_kwargs: [changed])
        monkeypatch.setattr(dispatcher.gateway, "click_group_bot_confirmation_button", lambda *_args, **_kwargs: pytest.fail("must not click old action"))

        payload = GroupBotConfirmationButtonPayload.model_validate(confirmation.payload)
        assert dispatcher._dispatch_group_bot_confirmation_button(session, confirmation, account, None, payload)

        replacements = list(session.scalars(select(Action).where(Action.id != confirmation.id)))
        new_follows = [
            item for item in replacements
            if item.action_type == "group_bot_channel_follow"
            and item.payload["source_message_id"] == "prompt-2"
        ]
        assert confirmation.status == "skipped"
        assert [item.payload["channel_ref"] for item in new_follows] == ["beta"]
        assert admission.surface_identity["requirement_channel_refs"] == ["beta"]


def test_stale_admission_version_cannot_materialize_replacement_actions() -> None:
    session, _account, admission, confirmation = _scenario()
    with session:
        stale_version = int(admission.version)
        session.execute(
            update(TaskGroupBotAdmission)
            .where(TaskGroupBotAdmission.id == admission.id)
            .values(version=stale_version + 1),
            execution_options={"synchronize_session": False},
        )
        existing_ids = set(session.scalars(select(Action.id)))

        assert record_control_facts(session, admission, [_message("prompt-2")], end_cursor=102) == 0

        assert set(session.scalars(select(Action.id))) == existing_ids
        assert confirmation.status == "pending"
        assert admission.version == stale_version + 1

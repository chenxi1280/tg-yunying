from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.integrations.telegram import SendResult
from app.models import (
    Action,
    AiGroupMessageMemory,
    ExecutionAttempt,
    FailureType,
    MessageTask,
    Task,
    TaskStatus,
    Tenant,
    TgAccount,
    TgAccountOnlineState,
    TgGroup,
    TgGroupAccount,
)
from app.services._common import _now
from app.services.task_center import dispatcher
from app.services.task_center import group_send_limits
from app.services.task_center.dispatcher import claim_actions


def _seed_send_scope(
    session: Session,
    *,
    daily_limit: int,
    group_cooldown_seconds: int,
    now_value: datetime | None = None,
    prior_sent_at: datetime | None = None,
) -> Action:
    now_value = now_value or _now()
    prior_sent_at = prior_sent_at or now_value - timedelta(minutes=2)
    group = TgGroup(
        id=7,
        tenant_id=1,
        tg_peer_id="-1007",
        title="运营群",
        auth_status="已授权运营",
        can_send=True,
        daily_limit=daily_limit,
        group_cooldown_seconds=group_cooldown_seconds,
        require_review=False,
    )
    session.add_all([
        Tenant(id=1, name="默认运营空间"),
        group,
        TgAccount(id=11, tenant_id=1, display_name="历史账号", phone_masked="+861***0011", status="在线"),
        TgAccount(id=12, tenant_id=1, display_name="当前账号", phone_masked="+861***0012", status="在线", session_ciphertext="session-current"),
        Task(id="prior-task", tenant_id=1, name="历史发送", type="group_ai_chat", status="running"),
        Task(id="current-task", tenant_id=1, name="当前发送", type="group_ai_chat", status="running"),
    ])
    session.flush()
    session.add_all([
        TgGroupAccount(tenant_id=1, group_id=group.id, account_id=11, can_send=True),
        TgGroupAccount(tenant_id=1, group_id=group.id, account_id=12, can_send=True),
        TgAccountOnlineState(
            tenant_id=1,
            account_id=12,
            desired_online=True,
            online_status="online",
            stale_after_at=now_value + timedelta(minutes=5),
        ),
        AiGroupMessageMemory(
            id="current-memory",
            tenant_id=1,
            group_id=group.id,
            task_id="current-task",
            account_id=12,
            raw_text="当前发送内容",
            normalized_text="当前发送内容",
            text_fingerprint="current-memory",
            status="reserved",
            planned_at=now_value,
        ),
        Action(
            id="prior-action",
            tenant_id=1,
            task_id="prior-task",
            task_type="group_ai_chat",
            action_type="send_message",
            account_id=11,
            scheduled_at=now_value - timedelta(minutes=2),
            executed_at=prior_sent_at,
            status="success",
            payload={"group_id": group.id, "message_text": "历史发送内容"},
        ),
        Action(
            id="current-action",
            tenant_id=1,
            task_id="current-task",
            task_type="group_ai_chat",
            action_type="send_message",
            account_id=12,
            scheduled_at=now_value,
            status="pending",
            payload={
                "group_id": group.id,
                "message_text": "当前发送内容",
                "review_approved": True,
                "slot_id": "current-task:cycle:1:turn:1",
                "ai_message_memory_id": "current-memory",
            },
        ),
    ])
    session.flush()
    session.add(
        ExecutionAttempt(
            tenant_id=1,
            action_id="prior-action",
            account_id=11,
            attempt_no=1,
            status="success",
            before_call_at=prior_sent_at,
            gateway_call_started_at=prior_sent_at,
            after_call_at=prior_sent_at,
            remote_message_id="tg-prior",
        )
    )
    session.commit()
    return session.get(Action, "current-action")


def _dispatch_current_action(session: Session, monkeypatch: pytest.MonkeyPatch) -> Action:
    monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(
        dispatcher.gateway,
        "send_message",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("群限额拦截前不应调用 Telegram")),
    )
    [claimed] = claim_actions(session, limit=1, worker_id="group-limit-test")
    assert dispatcher.dispatch_action(session, claimed) is True
    return session.get(Action, "current-action")


@pytest.mark.no_postgres
def test_group_ai_send_respects_group_daily_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        _seed_send_scope(session, daily_limit=1, group_cooldown_seconds=0)

        action = _dispatch_current_action(session, monkeypatch)

        assert action.status == "pending"
        assert action.result["error_code"] == FailureType.SLOWMODE.value
        assert action.result["validation_stage"] == "group_send_limit"
        assert action.result["rate_limit_source"] == "group"
        assert action.result["retry_after_seconds"] > 0
        assert session.get(AiGroupMessageMemory, "current-memory").status == "reserved"


@pytest.mark.no_postgres
def test_group_ai_send_respects_group_cooldown(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        _seed_send_scope(session, daily_limit=2, group_cooldown_seconds=300)

        action = _dispatch_current_action(session, monkeypatch)

        assert action.status == "pending"
        assert action.result["error_code"] == FailureType.SLOWMODE.value
        assert action.result["validation_stage"] == "group_send_limit"
        assert action.result["rate_limit_source"] == "group"
        assert 0 < action.result["retry_after_seconds"] <= 300


@pytest.mark.no_postgres
def test_group_ai_send_counts_legacy_group_sends(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        _seed_send_scope(session, daily_limit=2, group_cooldown_seconds=0)
        session.add(MessageTask(
            tenant_id=1,
            group_id=7,
            account_id=11,
            content="旧消息发送已成功",
            target_type="group",
            status=TaskStatus.SENT.value,
            sent_at=_now(),
            idempotency_key="legacy-group-send-limit",
        ))
        session.commit()

        action = _dispatch_current_action(session, monkeypatch)

        assert action.status == "pending"
        assert action.result["validation_stage"] == "group_send_limit"


@pytest.mark.no_postgres
def test_group_ai_send_cooldown_crosses_beijing_day(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = datetime(2026, 7, 22, 0, 1)
    monkeypatch.setattr(dispatcher, "_now", lambda: now_value)
    monkeypatch.setattr(group_send_limits, "_now", lambda: now_value)
    monkeypatch.setattr(dispatcher, "_group_ai_account_online_ready", lambda *_args: True)

    with Session(engine) as session:
        _seed_send_scope(
            session,
            daily_limit=2,
            group_cooldown_seconds=300,
            now_value=now_value,
            prior_sent_at=datetime(2026, 7, 21, 23, 59),
        )

        action = _dispatch_current_action(session, monkeypatch)

        assert action.status == "pending"
        assert action.result["validation_stage"] == "group_send_limit"
        assert 0 < action.result["retry_after_seconds"] <= 300

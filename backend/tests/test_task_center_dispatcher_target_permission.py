from __future__ import annotations

from datetime import timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.integrations.telegram import OperationResult, SendResult
from app.models import Action, AiGroupMessageMemory, GroupContextMessage, OperationTarget, Task, Tenant, TgAccount, TgAccountOnlineState, TgGroup, TgGroupAccount
from app.services._common import _now
from app.services.task_center import dispatcher
from app.services.task_center.dispatcher import claim_actions


def _add_group_ai_gate_payload(
    session: Session,
    now_value,
    *,
    action_id: str,
    task_id: str,
    group_id: int,
    account_id: int,
    text: str,
) -> dict:
    session.add(
        TgAccountOnlineState(
            tenant_id=1,
            account_id=account_id,
            desired_online=True,
            online_status="online",
            stale_after_at=now_value + timedelta(minutes=5),
        )
    )
    memory_id = f"memory-{action_id}"
    session.add(
        AiGroupMessageMemory(
            id=memory_id,
            tenant_id=1,
            group_id=group_id,
            task_id=task_id,
            account_id=account_id,
            raw_text=text,
            normalized_text=text,
            text_fingerprint=memory_id,
            status="reserved",
            planned_at=now_value,
        )
    )
    return {"slot_id": f"{task_id}:cycle:test:turn:{action_id}", "ai_message_memory_id": memory_id}


def test_peer_invalid_marks_group_account_not_sendable(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(Task(id="task-peer-invalid", tenant_id=1, name="peer invalid", type="group_ai_chat", status="running"))
        session.add(TgAccount(id=11, tenant_id=1, display_name="账号", phone_masked="+861***0011", status="在线", session_ciphertext="session"))
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="运营群", auth_status="已授权运营", can_send=True, require_review=False))
        session.add(TgGroupAccount(tenant_id=1, group_id=7, account_id=11, can_send=True, permission_label="可发言"))
        gate_payload = _add_group_ai_gate_payload(
            session,
            now_value,
            action_id="action-peer-invalid",
            task_id="task-peer-invalid",
            group_id=7,
            account_id=11,
            text="hello",
        )
        session.add(
            Action(
                id="action-peer-invalid",
                tenant_id=1,
                task_id="task-peer-invalid",
                task_type="group_ai_chat",
                action_type="send_message",
                account_id=11,
                status="pending",
                scheduled_at=now_value,
                payload={"group_id": 7, "message_text": "hello", "review_approved": True, **gate_payload},
            )
        )
        session.commit()

        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *args, **kwargs: object())
        monkeypatch.setattr(
            dispatcher.gateway,
            "send_message",
            lambda *args, **kwargs: SendResult(False, failure_type="目标无效", detail="目标实体无法解析"),
        )

        [claimed] = claim_actions(session, limit=1, worker_id="worker-test")
        assert dispatcher.dispatch_action(session, claimed) is True

        action = session.get(Action, "action-peer-invalid")
        link = session.scalar(select(TgGroupAccount).where(TgGroupAccount.group_id == 7, TgGroupAccount.account_id == 11))
        assert action.status == "failed"
        assert action.result["error_code"] == "目标无效"
        assert link is not None
        assert link.can_send is False
        assert link.permission_label == "目标实体无法解析"


def test_send_message_follows_required_channel_from_group_prompt_before_sending(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()
    calls: list[str] = []

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(
            OperationTarget(
                id=70,
                tenant_id=1,
                target_type="group",
                tg_peer_id="-1007",
                title="天津音乐学院",
                auth_status="已授权运营",
                can_send=True,
            )
        )
        session.add(
            Task(
                id="task-required-channel-prompt",
                tenant_id=1,
                name="天津 AI 活群",
                type="group_ai_chat",
                status="running",
                type_config={"auto_follow_required_channel": True},
            )
        )
        session.add(
            TgAccount(
                id=11,
                tenant_id=1,
                display_name="蕉大等风来 Clementine",
                username="clementine",
                phone_masked="+861***0011",
                status="在线",
                session_ciphertext="session",
            )
        )
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="天津音乐学院", auth_status="已授权运营", can_send=True))
        session.add(TgGroupAccount(tenant_id=1, group_id=7, account_id=11, can_send=True, permission_label="可发言"))
        gate_payload = _add_group_ai_gate_payload(
            session,
            now_value,
            action_id="action-required-channel-prompt",
            task_id="task-required-channel-prompt",
            group_id=7,
            account_id=11,
            text="hello",
        )
        session.add(
            GroupContextMessage(
                tenant_id=1,
                group_id=7,
                listener_account_id=11,
                sender_name="学院助手",
                sender_username="college_bot",
                is_bot=True,
                content="蕉大等风来 Clementine，您需要关注我们的频道才能发言。",
                remote_message_id="required-channel-1",
                sent_at=now_value,
            )
        )
        session.add(
            Action(
                id="action-required-channel-prompt",
                tenant_id=1,
                task_id="task-required-channel-prompt",
                task_type="group_ai_chat",
                action_type="send_message",
                account_id=11,
                status="pending",
                scheduled_at=now_value,
                payload={
                    "group_id": 7,
                    "operation_target_id": 70,
                    "target_display": "天津音乐学院",
                    "message_text": "hello",
                    "review_approved": True,
                    **gate_payload,
                },
            )
        )
        session.commit()

        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *args, **kwargs: object())
        monkeypatch.setattr(
            dispatcher.gateway,
            "ensure_linked_channel_membership",
            lambda *args, **kwargs: calls.append("follow_linked") or OperationResult(True, "已处理", detail="已关注关联频道"),
        )
        monkeypatch.setattr(
            dispatcher.gateway,
            "probe_target_capabilities",
            lambda *args, **kwargs: calls.append("probe") or OperationResult(True, detail="可发言"),
        )

        def fail_if_send_called(*_args, **_kwargs):
            raise AssertionError("send_message must wait until required-channel follow is confirmed")

        monkeypatch.setattr(dispatcher.gateway, "send_message", fail_if_send_called)

        [claimed] = claim_actions(session, limit=1, worker_id="worker-test")
        assert dispatcher.dispatch_action(session, claimed) is True

        action = session.get(Action, "action-required-channel-prompt")
        link = session.scalar(select(TgGroupAccount).where(TgGroupAccount.group_id == 7, TgGroupAccount.account_id == 11))
        assert calls == ["follow_linked", "probe"]
        assert action.status == "pending"
        assert action.result["error_code"] == "required_channel_followed_retry"
        assert action.result["prerequisite_channel_followed"] is True
        assert link is not None
        assert link.can_send is True

        monkeypatch.setattr(dispatcher.gateway, "send_message", lambda *args, **kwargs: SendResult(True, remote_message_id="tg-ok"))
        calls.clear()
        [claimed_again] = claim_actions(session, limit=1, worker_id="worker-test")
        assert dispatcher.dispatch_action(session, claimed_again) is True
        session.flush()
        session.refresh(action)

    assert calls == []
    assert action.status == "success"
    assert action.result["telegram_msg_id"] == "tg-ok"


def test_send_message_waits_when_required_channel_admission_is_pending(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(Task(id="task-admission-pending", tenant_id=1, name="admission pending", type="group_ai_chat", status="running"))
        session.add(TgAccount(id=11, tenant_id=1, display_name="账号", phone_masked="+861***0011", status="在线", session_ciphertext="session"))
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="天津音乐学院", auth_status="已授权运营", can_send=True, require_review=False))
        session.add(
            TgGroupAccount(
                tenant_id=1,
                group_id=7,
                account_id=11,
                can_send=False,
                permission_label="待关注必需频道后复检:学院助手提示",
            )
        )
        session.add(
            Action(
                id="action-admission-pending",
                tenant_id=1,
                task_id="task-admission-pending",
                task_type="group_ai_chat",
                action_type="send_message",
                account_id=11,
                status="pending",
                scheduled_at=now_value,
                payload={"group_id": 7, "message_text": "hello", "review_approved": True},
            )
        )
        session.commit()

        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *args, **kwargs: object())

        def fail_if_send_called(*_args, **_kwargs):
            raise AssertionError("send_message must wait for required-channel admission")

        monkeypatch.setattr(dispatcher.gateway, "send_message", fail_if_send_called)

        [claimed] = claim_actions(session, limit=1, worker_id="worker-test")
        assert dispatcher.dispatch_action(session, claimed) is True
        action = session.get(Action, "action-admission-pending")

    assert action.status == "pending"
    assert action.result["error_code"] == "required_channel_admission_pending"
    assert action.result["validation_stage"] == "required_channel_follow"
    assert action.scheduled_at > now_value

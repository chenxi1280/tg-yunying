from __future__ import annotations

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.integrations.telegram import SendResult
from app.models import Action, Task, Tenant, TgAccount, TgGroup, TgGroupAccount
from app.services._common import _now
from app.services.task_center import dispatcher
from app.services.task_center.dispatcher import claim_actions


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
                payload={"group_id": 7, "message_text": "hello", "review_approved": True},
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

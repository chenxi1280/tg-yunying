from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Action, GroupContextMessage, Task, Tenant, TgAccount, TgGroup, TgGroupAccount
from app.services.task_center.ai_generation_dependencies import GenerationDependencies
from app.services.task_center.payloads import SendMessagePayload


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return Session(engine)


def _seed_scope(session: Session) -> None:
    now_value = datetime.now(UTC)
    session.add(Tenant(id=1, name="租户"))
    session.add_all([
        TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="A群"),
        TgGroup(id=8, tenant_id=1, tg_peer_id="-1008", title="B群"),
        TgAccount(
            id=11,
            tenant_id=1,
            display_name="账号",
            phone_masked="***11",
            status="在线",
            session_ciphertext="session",
        ),
        TgGroupAccount(tenant_id=1, group_id=7, account_id=11, can_send=True),
        TgGroupAccount(tenant_id=1, group_id=8, account_id=11, can_send=True),
        Task(
            id="task-b",
            tenant_id=1,
            name="B群任务",
            type="group_ai_chat",
            status="running",
            type_config={"target_group_id": 8},
        ),
        GroupContextMessage(
            id=701,
            tenant_id=1,
            group_id=7,
            listener_account_id=11,
            content="A群内容",
            remote_message_id="a-1",
            sent_at=now_value,
        ),
        GroupContextMessage(
            id=801,
            tenant_id=1,
            group_id=8,
            listener_account_id=11,
            content="B群内容",
            remote_message_id="b-1",
            sent_at=now_value,
        ),
    ])
    session.commit()


def _payload(**updates) -> SendMessagePayload:
    data = {
        "chat_id": "-1008",
        "group_id": 8,
        "message_text": "",
        "ai_generation_status": "pending",
        "chat_mode": "reply",
        "content_scope_contract_version": "group_content_scope_v1",
        "content_scope_tenant_id": 1,
        "content_scope_group_id": 8,
        "content_scope_task_id": "task-b",
    }
    data.update(updates)
    return SendMessagePayload.model_validate(data)


def _action(payload: SendMessagePayload, *, action_id: str = "action-b") -> Action:
    return Action(
        id=action_id,
        tenant_id=1,
        task_id="task-b",
        task_type="group_ai_chat",
        action_type="send_message",
        account_id=11,
        status="executing",
        payload=payload.model_dump(mode="json"),
    )


def _forbidden_dependencies(calls: dict[str, int]) -> GenerationDependencies:
    def forbidden(*_args, **_kwargs):
        calls["provider"] += 1
        raise AssertionError("scope mismatch must stop before provider")

    return GenerationDependencies(
        normal_generator=forbidden,
        reply_generator=forbidden,
        reply_target_probe=forbidden,
        reply_messages_fetcher=forbidden,
    )

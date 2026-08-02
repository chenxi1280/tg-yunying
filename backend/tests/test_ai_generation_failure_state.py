from __future__ import annotations

from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Action, GroupContextMessage, Task, Tenant, TgAccount, TgAccountOnlineState, TgGroup, TgGroupAccount
from app.services._common import _now
from app.services.task_center import dispatcher
from app.services.task_center.ai_generation_dependencies import GenerationDependencies
from app.services.task_center.ai_generator import AiGenerationUnavailable, GeneratedContent
from app.services.task_center.ai_generation_worker import drain_ai_generation
from app.services.task_center.conversation_speaker_rotation import (
    conversation_key_for_group,
    lock_or_create_state,
    reserve_speaker_turn,
)


pytestmark = pytest.mark.no_postgres


def test_quality_rejection_preserves_failure_and_skips_gateway(monkeypatch) -> None:
    observed = {"gateway_calls": 0}
    with _action_session() as (session, action):
        monkeypatch.setattr(
            "app.services.task_center.ai_generation_worker.credentials_for_account",
            lambda *_args, **_kwargs: object(),
        )
        monkeypatch.setattr(dispatcher.gateway, "send_message", _gateway_sender(observed))

        assert drain_ai_generation(
            lambda: Session(session.get_bind()),
            limit=1,
            dependencies=_dependencies(_hallucinated_generator),
        ) == 1
        session.refresh(action)

        assert action.status == "failed"
        assert action.result["error_code"] == "hallucination_risk"
        assert action.result["generation_outcome"] == "hallucination_risk"
        assert observed["gateway_calls"] == 0


def test_provider_failure_keeps_generic_code_and_specific_detail(monkeypatch) -> None:
    observed = {"gateway_calls": 0}
    with _action_session() as (session, action):
        monkeypatch.setattr(
            "app.services.task_center.ai_generation_worker.credentials_for_account",
            lambda *_args, **_kwargs: object(),
        )
        monkeypatch.setattr(dispatcher.gateway, "send_message", _gateway_sender(observed))

        assert drain_ai_generation(
            lambda: Session(session.get_bind()),
            limit=1,
            dependencies=_dependencies(_unavailable_generator),
        ) == 1
        session.refresh(action)

        assert action.status == "failed"
        assert action.result["error_code"] == "ai_generation_failed"
        assert "租户 AI 配置不存在" in action.result["error_message"]
        assert observed["gateway_calls"] == 0


def test_generation_failure_releases_existing_speaker_reservation(monkeypatch) -> None:
    with _action_session() as (session, action):
        monkeypatch.setattr(
            "app.services.task_center.ai_generation_worker.credentials_for_account",
            lambda *_args, **_kwargs: object(),
        )
        key = conversation_key_for_group(group_id=7)
        reserve_speaker_turn(
            session,
            action=action,
            surface="group_ai_chat",
            conversation_key=key,
            candidate_account_ids=[11],
        )
        session.commit()

        assert drain_ai_generation(
            lambda: Session(session.get_bind()),
            limit=1,
            dependencies=_dependencies(_unavailable_generator),
        ) == 1
        session.expire_all()
        state = lock_or_create_state(
            session,
            tenant_id=1,
            surface="group_ai_chat",
            conversation_key=key,
        )

        assert state.reserved_action_id is None
        assert state.reserved_account_id is None


@contextmanager
def _action_session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session, _seed_action(session)
    engine.dispose()


def _seed_action(session: Session) -> Action:
    now_value = _now()
    _seed_scope(session, now_value)
    action = Action(
        id="action-generation-failure",
        tenant_id=1,
        task_id="task-generation-failure",
        task_type="group_ai_chat",
        action_type="send_message",
        account_id=11,
        status="pending",
        scheduled_at=now_value,
        payload=_pending_payload(),
    )
    session.add(action)
    session.commit()
    return action


def _seed_scope(session: Session, now_value) -> None:
    session.add(Tenant(id=1, name="tenant"))
    session.add(Task(
        id="task-generation-failure",
        tenant_id=1,
        name="generation failure",
        type="group_ai_chat",
        status="running",
        type_config={"target_group_id": 7, "ai_model": "MiMo-V2.5"},
    ))
    session.add(TgAccount(
        id=11,
        tenant_id=1,
        display_name="账号A",
        phone_masked="+8611",
        status="在线",
        session_ciphertext="session-a",
    ))
    session.add(TgAccountOnlineState(
        tenant_id=1,
        account_id=11,
        desired_online=True,
        online_status="online",
        last_seen_at=now_value,
        stale_after_at=now_value,
    ))
    session.add(TgGroup(
        id=7,
        tenant_id=1,
        tg_peer_id="-1007",
        title="运营群",
        auth_status="已授权运营",
        can_send=True,
        require_review=False,
        listener_enabled=True,
        listener_last_polled_at=now_value,
        listener_remote_cursor="701",
        listener_cursor_status="contiguous",
    ))
    session.add(TgGroupAccount(tenant_id=1, group_id=7, account_id=11, can_send=True))
    session.add(GroupContextMessage(
        id=701,
        tenant_id=1,
        group_id=7,
        listener_account_id=11,
        sender_name="真人用户",
        content="今天按原计划吗？",
        remote_message_id="701",
        sent_at=now_value,
    ))


def _pending_payload() -> dict:
    return {
        "group_id": 7,
        "target_display": "运营群",
        "message_text": "",
        "review_approved": True,
        "cycle_id": "cycle-failure",
        "slot_id": "cycle-failure:turn:1",
        "turn_index": 1,
        "ai_generation_id": "cycle-failure",
        "ai_generation_status": "pending",
        "ai_generation_history": "真人用户: 今天按原计划吗？",
        "chat_id": "-1007",
        "chat_mode": "idle_warmup",
        "anchor_message_ids": [701],
        "context_message_ids": [701],
        "context_snapshot_message_id": 701,
        "content_scope_contract_version": "group_content_scope_v1",
        "content_scope_tenant_id": 1,
        "content_scope_group_id": 7,
        "content_scope_task_id": "task-generation-failure",
    }


def _dependencies(normal_generator) -> GenerationDependencies:
    return GenerationDependencies(
        normal_generator=normal_generator,
        reply_generator=_forbidden_external,
        reply_target_probe=_forbidden_external,
        reply_message_fetcher=_forbidden_external,
    )


def _hallucinated_generator(_session, _tenant_id, config, **_kwargs):
    slot = config["generation_slots"][0]
    return [GeneratedContent("我上次准点到", slot_id=slot["slot_id"], sequence_index=1)], 1


def _unavailable_generator(*_args, **_kwargs):
    raise AiGenerationUnavailable("租户 AI 配置不存在")


def _gateway_sender(observed: dict[str, int]):
    def send(*_args, **_kwargs):
        observed["gateway_calls"] += 1
        raise AssertionError("generation failure must not reach Telegram gateway")

    return send


def _forbidden_external(*_args, **_kwargs):
    raise AssertionError("unexpected external call")

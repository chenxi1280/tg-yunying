from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    Action,
    GenerationJob,
    GroupBotAdmission,
    Task,
    TaskGroupBotAdmission,
    Tenant,
    TgAccount,
    TgAccountAuthorization,
    TgGroup,
)
from app.services._common import _now
from app.services.task_center import ai_generation_worker
from app.services.task_center.payloads import SendMessagePayload


pytestmark = pytest.mark.no_postgres


def test_send_message_payload_accepts_frozen_task_admission_fact() -> None:
    payload = SendMessagePayload.model_validate({
        "group_id": 17,
        "ai_generation_status": "pending",
        "task_group_bot_admission_id": "admission-1",
        "task_group_bot_admission_version": 2,
    })

    assert payload.task_group_bot_admission_id == "admission-1"
    assert payload.task_group_bot_admission_version == 2


def test_generation_worker_defers_admission_driver_before_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        task, _account, action = _seed_waiting_action(session)
        task_id = task.id
        action_id = action.id
        monkeypatch.setattr(
            ai_generation_worker,
            "credentials_for_account",
            lambda *_args, **_kwargs: pytest.fail("provider credentials must not load"),
        )
    processed = ai_generation_worker.drain_ai_generation(
        lambda: Session(engine),
        limit=1,
        dependencies=SimpleNamespace(),
    )

    with Session(engine) as session:
        task = session.get(Task, task_id)
        action = session.get(Action, action_id)
        assert processed == 1
        session.refresh(action)
        assert task.status == "running"
        assert action.status == "pending"
        assert action.claim_token == ""
        assert action.result["error_code"] == "group_bot_admission_wait"
        assert action.result["validation_stage"] == "pre_ai_group_bot_admission"
        assert action.scheduled_at > _now()


def test_fact_first_generation_uses_task_admission_before_provider(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'fact-first-c2.db'}", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Tenant(id=1, name="新合同准入生成门测试"))
        session.add(TgGroup(id=17, tenant_id=1, tg_peer_id="-10017", title="新合同群"))
        session.add(TgAccount(
            id=21,
            tenant_id=1,
            display_name="新合同账号",
            phone_masked="21",
            session_ciphertext="encrypted-session",
        ))
        session.add(TgAccountAuthorization(
            id=31,
            tenant_id=1,
            account_id=21,
            status="active",
            is_current=True,
        ))
        task = Task(
            id="fact-first-generation-admission",
            tenant_id=1,
            name="新合同生成前准入门",
            type="group_ai_chat",
            status="running",
            fulfillment_contract_version="fact_first_v3",
            type_config={
                "target_group_id": 17,
                "group_bot_admission_required": True,
            },
        )
        action = Action(
            id="fact-first-waiting-generation",
            tenant_id=1,
            task_id=task.id,
            task_type="group_ai_chat",
            action_type="send_message",
            account_id=21,
            status="pending",
            scheduled_at=_now(),
            obligation_type="quantity_slot",
            obligation_id="fact-first-c2-slot",
            payload={
                "group_id": 17,
                "message_text": "",
                "ai_generation_status": "pending",
                "group_bot_admission_state": "group_bot_policy_unresolved",
            },
        )
        session.add_all([task, action])
        session.commit()
    monkeypatch.setattr(
        ai_generation_worker,
        "credentials_for_account",
        lambda *_args, **_kwargs: pytest.fail("provider credentials must not load"),
    )

    processed = ai_generation_worker.drain_ai_generation(
        lambda: Session(engine),
        limit=1,
        dependencies=SimpleNamespace(),
    )

    with Session(engine) as session:
        action = session.get(Action, "fact-first-waiting-generation")
        admission = session.query(TaskGroupBotAdmission).one()
        job = session.query(GenerationJob).one()
        assert processed == 1
        assert action.status == "pending"
        assert action.result["error_code"] == "c2_observation_started"
        assert action.result["validation_stage"] == "pre_ai_task_group_bot_admission"
        assert admission.task_id == action.task_id
        assert admission.state == "observing"
        assert job.state == "pending"


def _seed_waiting_action(session: Session) -> tuple[Task, TgAccount, Action]:
    session.add(Tenant(id=1, name="准入生成门测试"))
    session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="测试群"))
    account = TgAccount(
        id=11,
        tenant_id=1,
        display_name="测试账号",
        phone_masked="11",
    )
    task = Task(
        id="ai-generation-admission-gate",
        tenant_id=1,
        name="生成前准入门",
        type="group_ai_chat",
        status="running",
        type_config={
            "target_group_id": 7,
            "group_bot_admission_required": True,
        },
    )
    session.add_all([account, task])
    session.flush()
    admission = GroupBotAdmission(
        tenant_id=1,
        group_id=7,
        account_id=11,
        state="group_bot_policy_unresolved",
    )
    action = Action(
        id="waiting-admission-generation",
        tenant_id=1,
        task_id=task.id,
        task_type="group_ai_chat",
        action_type="send_message",
        account_id=11,
        status="pending",
        scheduled_at=_now(),
        payload={
            "group_id": 7,
            "message_text": "",
            "ai_generation_status": "pending",
            "group_bot_admission_state": admission.state,
        },
    )
    session.add_all([admission, action])
    session.commit()
    return task, account, action

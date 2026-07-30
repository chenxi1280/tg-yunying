from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Action, Task, Tenant, TgAccount
from app.services._common import _now
from app.services.task_center.ai_generator import GeneratedContent
from app.services.task_center.ai_generation_worker import drain_ai_generation
from tests.ai_generation_phase_test_support import (
    generation_dependencies,
    normal_generator,
    seed_reserved_normal_batch,
)


pytestmark = pytest.mark.no_postgres


def test_generation_worker_prepares_pending_ai_before_dispatch() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    _seed_actions(engine)
    generated: list[str] = []

    def generate(session: Session, action: Action, _account: TgAccount) -> None:
        generated.append(action.id)
        action.payload = {
            **dict(action.payload or {}),
            "message_text": "生成完成",
            "ai_generation_status": "ready",
        }
        session.commit()

    processed = drain_ai_generation(
        lambda: Session(engine),
        limit=10,
        generate_action=generate,
    )

    assert processed == 1
    assert generated == ["pending-generation"]
    with Session(engine) as session:
        pending = session.get(Action, "pending-generation")
        ready = session.get(Action, "already-ready")
        assert pending.payload["ai_generation_status"] == "ready"
        assert ready.payload["message_text"] == "已有文案"


def test_generation_worker_does_not_prepare_far_future_actions() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    _seed_actions(engine)
    with Session(engine) as session:
        action = session.get(Action, "pending-generation")
        action.scheduled_at = _now() + timedelta(hours=2)
        session.commit()

    processed = drain_ai_generation(
        lambda: Session(engine),
        limit=10,
        generate_action=lambda *_args: pytest.fail("far future action generated"),
    )

    assert processed == 0


def test_generation_worker_skips_account_with_executing_action() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    _seed_actions(engine)
    with Session(engine) as session:
        session.add(TgAccount(
            id=12,
            tenant_id=1,
            display_name="AI账号2",
            phone_masked="+861***0012",
            status="在线",
        ))
        occupied = _action("occupied-account", _now(), "正在发送", "ready")
        occupied.status = "executing"
        available = _action(
            "available-account",
            _now() + timedelta(seconds=1),
            "",
            "pending",
        )
        available.account_id = 12
        session.add_all([occupied, available])
        session.commit()
    generated: list[str] = []

    def generate(session: Session, action: Action, _account: TgAccount) -> None:
        generated.append(action.id)
        action.payload = {
            **dict(action.payload or {}),
            "message_text": "可用账号生成完成",
            "ai_generation_status": "ready",
        }
        session.commit()

    assert drain_ai_generation(
        lambda: Session(engine),
        limit=1,
        generate_action=generate,
    ) == 1
    assert generated == ["available-account"]


def test_production_generation_pipeline_returns_batch_to_pending_dispatch(
    monkeypatch,
) -> None:
    from app.services.task_center import ai_generation_worker

    monkeypatch.setattr(
        ai_generation_worker,
        "credentials_for_account",
        lambda *_args, **_kwargs: object(),
    )
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        actions, _coverages = seed_reserved_normal_batch(
            session,
            _now(),
            bind_coverage=False,
        )
        for action in actions:
            action.status = "pending"
            action.claim_owner = ""
            action.claim_token = ""
            action.payload = {
                **dict(action.payload or {}),
                "ai_generation_claim_owner": "",
                "ai_generation_claim_token": "",
            }
        session.commit()
        dependencies = generation_dependencies(normal_generator=normal_generator(
            session,
            [
                GeneratedContent(
                    "一号",
                    slot_id="cycle-normal:turn:1",
                    sequence_index=1,
                ),
                GeneratedContent(
                    "二号",
                    slot_id="cycle-normal:turn:2",
                    sequence_index=2,
                ),
            ],
        ))

    processed = drain_ai_generation(
        lambda: Session(engine),
        limit=10,
        dependencies=dependencies,
    )

    assert processed == 2
    with Session(engine) as session:
        actions = list(session.scalars(
            select(Action).order_by(Action.id),
        ))
        assert [action.status for action in actions] == ["pending", "pending"]
        assert {action.payload["message_text"] for action in actions} == {"一号", "二号"}
        assert all(action.payload["ai_generation_status"] == "ready" for action in actions)


def test_generation_worker_keeps_recovery_status_in_same_normal_batch(
    monkeypatch,
) -> None:
    from app.services.task_center import ai_generation_worker

    monkeypatch.setattr(
        ai_generation_worker,
        "credentials_for_account",
        lambda *_args, **_kwargs: object(),
    )
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        actions, _coverages = seed_reserved_normal_batch(
            session,
            _now(),
            bind_coverage=False,
        )
        for action in actions:
            action.status = "pending"
            action.claim_owner = ""
            action.claim_token = ""
        actions[1].payload = {
            **dict(actions[1].payload or {}),
            "ai_generation_status": "ai_result_persist_unknown",
        }
        session.commit()
        dependencies = generation_dependencies(normal_generator=normal_generator(
            session,
            [
                GeneratedContent(
                    "恢复一号",
                    slot_id="cycle-normal:turn:1",
                    sequence_index=1,
                ),
                GeneratedContent(
                    "恢复二号",
                    slot_id="cycle-normal:turn:2",
                    sequence_index=2,
                ),
            ],
        ))

    assert drain_ai_generation(
        lambda: Session(engine),
        limit=10,
        dependencies=dependencies,
    ) == 2

    with Session(engine) as session:
        actions = list(session.scalars(select(Action).order_by(Action.id)))
        assert all(action.status == "pending" for action in actions)
        assert all(
            action.payload["ai_generation_status"] == "ready"
            for action in actions
        )


def _seed_actions(engine) -> None:
    now_value = _now()
    with Session(engine) as session:
        session.add(Tenant(id=1, name="tenant"))
        session.add(TgAccount(
            id=11,
            tenant_id=1,
            display_name="AI账号",
            phone_masked="+861***0011",
            status="在线",
        ))
        session.add(Task(
            id="ai-task",
            tenant_id=1,
            name="AI活群",
            type="group_ai_chat",
            status="running",
        ))
        session.add_all([
            _action("pending-generation", now_value, "", "pending"),
            _action("already-ready", now_value, "已有文案", "ready"),
        ])
        session.commit()


def _action(action_id: str, scheduled_at, message_text: str, generation_status: str) -> Action:
    return Action(
        id=action_id,
        tenant_id=1,
        task_id="ai-task",
        task_type="group_ai_chat",
        action_type="send_message",
        account_id=11,
        status="pending",
        scheduled_at=scheduled_at,
        payload={
            "message_text": message_text,
            "ai_generation_status": generation_status,
        },
    )

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Action, Task, Tenant, TgAccount, TgGroup
from app.services._common import _now
from app.services.task_center.ai_generator import AiGenerationUnavailable, GeneratedContent
from app.services.task_center.ai_generation_quality import fail_generation_action
from app.services.task_center.ai_generation_worker import (
    GenerationAdmissionDeferred,
    drain_ai_generation,
)
from tests.ai_generation_phase_test_support import (
    generation_dependencies,
    seed_reserved_normal_batch,
)


pytestmark = pytest.mark.no_postgres


def _single_action_generator(session: Session, content_by_slot: dict[str, str]):
    def generate(_session, _tenant_id, config, *, count, **_kwargs):
        assert session.in_transaction() is False
        assert count == 1
        [slot] = config["generation_slots"]
        slot_id = str(slot["slot_id"])
        return [GeneratedContent(
            content_by_slot[slot_id],
            slot_id=slot_id,
            sequence_index=1,
        )], 7

    return generate


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


def test_generation_worker_claims_sibling_outside_window_in_later_batch() -> None:
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
        first = session.get(Action, "pending-generation")
        first.payload = {**dict(first.payload or {}), "ai_generation_id": "shared"}
        future = _action(
            "pending-generation-future",
            _now() + timedelta(minutes=5),
            "",
            "pending",
        )
        future.account_id = 12
        future.payload = {**dict(future.payload or {}), "ai_generation_id": "shared"}
        session.add(future)
        session.commit()

    generated: list[str] = []

    def generate(session: Session, action: Action, _account: TgAccount) -> None:
        generated.append(action.id)
        action.payload = {
            **dict(action.payload or {}),
            "message_text": "当前窗口生成完成",
            "ai_generation_status": "ready",
        }
        session.commit()

    assert drain_ai_generation(
        lambda: Session(engine),
        limit=10,
        generate_action=generate,
    ) == 1
    assert generated == ["pending-generation"]
    with Session(engine) as session:
        future = session.get(Action, "pending-generation-future")
        assert future.status == "pending"
        assert future.claim_owner == ""
        assert future.payload["message_text"] == ""


def test_generation_worker_generates_other_group_while_ready_group_waits() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    _seed_actions(engine)
    with Session(engine) as session:
        ready = session.get(Action, "already-ready")
        ready.payload = {**dict(ready.payload or {}), "group_id": 7}
        pending = session.get(Action, "pending-generation")
        pending.scheduled_at = _now() + timedelta(seconds=1)
        other = _action("other-group", _now(), "", "pending")
        other.payload = {**dict(other.payload or {}), "group_id": 8}
        session.add(other)
        session.commit()
    generated: list[str] = []

    def generate(session: Session, action: Action, _account: TgAccount) -> None:
        generated.append(action.id)
        action.payload = {
            **dict(action.payload or {}),
            "message_text": "另一群生成完成",
            "ai_generation_status": "ready",
        }
        session.commit()

    assert drain_ai_generation(
        lambda: Session(engine),
        limit=10,
        generate_action=generate,
    ) == 1
    assert generated == ["other-group"]


def test_generation_claim_blocks_same_group_before_provider_starts() -> None:
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
        second = _action("same-group-second", _now(), "", "pending")
        second.account_id = 12
        session.add(second)
        session.commit()

    observed_status: list[str] = []

    def generate(session: Session, action: Action, _account: TgAccount) -> None:
        observed_status.append(action.payload["ai_generation_status"])
        nested = drain_ai_generation(
            lambda: Session(engine),
            limit=1,
            generate_action=lambda *_args: pytest.fail("same group claimed twice"),
        )
        assert nested == 0
        action.payload = {
            **dict(action.payload or {}),
            "message_text": "第一条完成",
            "ai_generation_status": "ready",
        }
        session.commit()

    assert drain_ai_generation(
        lambda: Session(engine),
        limit=1,
        generate_action=generate,
    ) == 1
    assert observed_status == ["generating"]


def test_due_catch_up_pipeline_stops_at_configured_depth(monkeypatch) -> None:
    from app.services.task_center import ai_generation_worker

    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    _seed_actions(engine)
    with Session(engine) as session:
        session.add_all([
            _action("catch-up-second", _now(), "", "pending"),
            _action("catch-up-third", _now(), "", "pending"),
        ])
        session.commit()

    monkeypatch.setattr(
        ai_generation_worker,
        "_due_catch_up_pipeline_depth",
        lambda *_args: 2,
    )

    def generate(session: Session, action: Action, _account: TgAccount) -> None:
        action.payload = {
            **dict(action.payload or {}),
            "message_text": "签到",
            "ai_generation_status": "ready",
            "content_source": "due_catch_up_check_in",
            "generation_source": "static_safe_fallback",
            "quality_fallback": "check_in_fallback",
            "fallback_reason": "due_catch_up_provider_budget_exhausted",
            "coverage_ledger_id": f"coverage-{action.id}",
            "daily_group_target_id": "daily-target-catch-up",
            "primary_quantity_slot_id": f"quantity-{action.id}",
        }
        session.commit()

    processed = drain_ai_generation(
        lambda: Session(engine),
        limit=10,
        generate_action=generate,
    )

    assert processed == 2
    with Session(engine) as session:
        actions = list(session.scalars(select(Action).where(
            Action.payload["group_id"].as_integer() == 7,
        )))
        ready = [
            action for action in actions
            if action.payload.get("ai_generation_status") == "ready"
        ]
        pending = [
            action for action in actions
            if action.payload.get("ai_generation_status") == "pending"
        ]
        assert len(ready) == 2
        assert len(pending) == 1


def test_due_catch_up_pipeline_depth_requires_full_runtime_eligibility(monkeypatch) -> None:
    from app.services.task_center import ai_generation_worker

    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    _seed_actions(engine)
    monkeypatch.setattr(
        ai_generation_worker,
        "_content_obligation_fallback_ready",
        lambda *_args: True,
    )
    monkeypatch.setattr(
        ai_generation_worker,
        "_due_catch_up_required",
        lambda *_args: True,
    )
    monkeypatch.setattr(
        ai_generation_worker,
        "tenant_fallback_flags",
        lambda *_args: {"_ai_group_static_fallback_enabled": True},
    )
    with Session(engine) as session:
        task = session.get(Task, "ai-task")
        task.type_config = {"due_catch_up_pipeline_depth": 4}
        action = session.get(Action, "pending-generation")
        action.primary_quantity_slot_id = "quantity-catch-up"
        action.payload = {
            **dict(action.payload or {}),
            "coverage_ledger_id": "coverage-catch-up",
            "daily_group_target_id": "daily-target-catch-up",
        }
        session.flush()

        assert ai_generation_worker._due_catch_up_pipeline_depth(session, action) == 4

        task.type_config = {
            "due_catch_up_pipeline_depth": 4,
            "ai_model": "explicit-model",
        }
        session.flush()

        assert ai_generation_worker._due_catch_up_pipeline_depth(session, action) == 1


def test_deferred_admission_releases_generating_claim_to_pending() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    _seed_actions(engine)

    def defer(*_args) -> None:
        raise GenerationAdmissionDeferred("pending-generation")

    assert drain_ai_generation(
        lambda: Session(engine),
        limit=1,
        generate_action=defer,
    ) == 1

    with Session(engine) as session:
        action = session.get(Action, "pending-generation")
        assert action.status == "pending"
        assert action.payload["ai_generation_status"] == "pending"
        assert action.claim_owner == ""
        assert action.lease_owner == ""


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
            group_id=9,
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
    with Session(engine) as session:
        blocked = session.get(Action, "pending-generation")
        assert blocked.status == "pending"
        assert blocked.payload["message_text"] == ""


def test_generation_worker_continues_after_explicit_business_failure() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    _seed_actions(engine)
    with Session(engine) as session:
        session.add(
            _action(
                "pending-generation-2",
                _now() + timedelta(seconds=1),
                "",
                "pending",
            ),
        )
        session.commit()

    def generate(session: Session, action: Action, _account: TgAccount) -> None:
        if action.id == "pending-generation":
            fail_generation_action(
                action,
                "duplicate_message",
                "显式业务失败",
                stage="ai_message_memory",
            )
            session.commit()
            raise AiGenerationUnavailable("duplicate_message")
        action.payload = {
            **dict(action.payload or {}),
            "message_text": "后续动作生成完成",
            "ai_generation_status": "ready",
        }
        session.commit()

    processed = drain_ai_generation(
        lambda: Session(engine),
        limit=2,
        generate_action=generate,
    )

    assert processed == 2
    with Session(engine) as session:
        failed = session.get(Action, "pending-generation")
        ready = session.get(Action, "pending-generation-2")
        assert failed.status == "failed"
        assert failed.claim_owner == ""
        assert failed.lease_owner == ""
        assert ready.status == "pending"
        assert ready.payload["message_text"] == "后续动作生成完成"


def test_generation_worker_settles_content_mix_after_persisted_failure(
    monkeypatch,
) -> None:
    from app.services.task_center import dispatcher

    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    _seed_actions(engine)
    settled: list[str] = []
    monkeypatch.setattr(
        dispatcher,
        "_sync_action_content_mix_state",
        lambda _session, action: settled.append(action.id),
    )

    def generate(session: Session, action: Action, _account: TgAccount) -> None:
        fail_generation_action(
            action,
            "duplicate_message",
            "显式业务失败",
            stage="ai_message_memory",
        )
        session.commit()
        raise AiGenerationUnavailable("duplicate_message")

    assert drain_ai_generation(
        lambda: Session(engine),
        limit=1,
        generate_action=generate,
    ) == 1
    assert settled == ["pending-generation"]


def test_generation_worker_exposes_unpersisted_generation_failure() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    _seed_actions(engine)

    with pytest.raises(AiGenerationUnavailable, match="provider_transport"):
        drain_ai_generation(
            lambda: Session(engine),
            limit=1,
            generate_action=lambda *_args: (
                _raise_generation_unavailable("provider_transport")
            ),
        )


def _raise_generation_unavailable(code: str) -> None:
    raise AiGenerationUnavailable(code)


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
        dependencies = generation_dependencies(normal_generator=_single_action_generator(
            session,
            {
                "cycle-normal:turn:1": "一号",
                "cycle-normal:turn:2": "二号",
            },
        ))

    processed = drain_ai_generation(
        lambda: Session(engine),
        limit=10,
        dependencies=dependencies,
    )

    assert processed == 1
    with Session(engine) as session:
        actions = list(session.scalars(
            select(Action)
            .where(Action.id != "source-reply-9001")
            .order_by(Action.id),
        ))
        assert [action.status for action in actions] == ["pending", "pending"]
        assert [bool(action.payload["message_text"]) for action in actions].count(True) == 1


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
        dependencies = generation_dependencies(normal_generator=_single_action_generator(
            session,
            {
                "cycle-normal:turn:1": "恢复一号",
                "cycle-normal:turn:2": "恢复二号",
            },
        ))

    assert drain_ai_generation(
        lambda: Session(engine),
        limit=10,
        dependencies=dependencies,
    ) == 1

    with Session(engine) as session:
        actions = list(session.scalars(
            select(Action)
            .where(Action.id != "source-reply-9001")
            .order_by(Action.id),
        ))
        assert all(action.status == "pending" for action in actions)
        assert [action.payload["ai_generation_status"] for action in actions].count("ready") == 1


def test_generation_worker_defers_unproven_listener_watermark_without_spinning(
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
        group = session.get(TgGroup, 7)
        group.listener_enabled = False
        for action in actions:
            action.status = "pending"
            action.claim_owner = ""
            action.claim_token = ""
        session.commit()

    processed = drain_ai_generation(
        lambda: Session(engine),
        limit=10,
        dependencies=generation_dependencies(),
    )

    assert processed == 2
    with Session(engine) as session:
        actions = list(session.scalars(
            select(Action)
            .where(Action.id != "source-reply-9001")
            .order_by(Action.id),
        ))
        assert all(action.status == "pending" for action in actions)
        assert all(
            action.result["error_code"] == "context_freshness_unproven"
            for action in actions
        )
        assert all(action.scheduled_at > _now() + timedelta(minutes=30) for action in actions)


def _seed_actions(engine) -> None:
    now_value = _now()
    with Session(engine) as session:
        session.add(Tenant(id=1, name="tenant"))
        session.add_all([
            TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="群7"),
            TgGroup(id=8, tenant_id=1, tg_peer_id="-1008", title="群8"),
            TgGroup(id=9, tenant_id=1, tg_peer_id="-1009", title="群9"),
        ])
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
            _action("already-ready", now_value, "已有文案", "ready", group_id=8),
        ])
        session.commit()


def _action(
    action_id: str,
    scheduled_at,
    message_text: str,
    generation_status: str,
    *,
    group_id: int = 7,
) -> Action:
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
            "group_id": group_id,
            "message_text": message_text,
            "ai_generation_status": generation_status,
        },
    )

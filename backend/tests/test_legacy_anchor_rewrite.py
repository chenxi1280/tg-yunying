from __future__ import annotations

from importlib import import_module
from importlib.util import find_spec

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Action, AiGroupMessageMemory, Task, TaskAccountDailyCoverage
from app.services._common import _now
from app.services.task_center import dispatcher
from app.services.task_center.executors import group_ai_chat


pytestmark = pytest.mark.no_postgres


def _contract_exemption_actions(task: Task) -> tuple[Action, Action, Action]:
    common = {
        "tenant_id": 1,
        "task_id": task.id,
        "task_type": "group_ai_chat",
        "action_type": "send_message",
        "status": "pending",
        "scheduled_at": _now(),
    }
    blueprint = Action(
        id="action-ungenerated-blueprint",
        account_id=11,
        payload={"group_id": 7, "message_text": "", "ai_generation_status": "pending"},
        **common,
    )
    check_in = Action(
        id="action-direct-check-in",
        account_id=12,
        payload={
            "group_id": 7,
            "message_text": "签到",
            "ai_generation_status": "ready",
            "generation_source": "direct_check_in",
        },
        **common,
    )
    current_provider = Action(
        id="action-current-provider-contract",
        account_id=13,
        payload={
            "group_id": 7,
            "message_text": "当前 style-only 合同正文",
            "ai_generation_status": "ready",
            "generation_source": "ai_provider",
            "voice_profile_contract_version": "style_only_v2",
        },
        **common,
    )
    return blueprint, check_in, current_provider


def test_planner_expires_legacy_anchor_rewritten_open_actions() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()
    with Session(engine) as session:
        task = Task(
            id="task-legacy-anchor",
            tenant_id=1,
            name="历史锚点改写",
            type="group_ai_chat",
            status="running",
        )
        memory = AiGroupMessageMemory(
            id="memory-legacy-anchor",
            tenant_id=1,
            group_id=7,
            task_id=task.id,
            action_id="action-legacy-anchor",
            account_id=11,
            raw_text="今天先聊聊 价格咋说",
            status="reserved",
        )
        action = Action(
            id="action-legacy-anchor",
            tenant_id=1,
            task_id=task.id,
            task_type="group_ai_chat",
            action_type="send_message",
            account_id=11,
            status="pending",
            scheduled_at=now_value,
            payload={
                "group_id": 7,
                "message_text": "今天先聊聊 价格咋说",
                "coverage_ledger_id": "coverage-legacy-anchor",
                "ai_message_memory_id": memory.id,
            },
            result={"voice_profile_anchor_rewritten": True},
        )
        coverage = TaskAccountDailyCoverage(
            id="coverage-legacy-anchor",
            tenant_id=1,
            task_id=task.id,
            group_id=7,
            account_id=11,
            coverage_date=now_value.date(),
            state="reserved",
            reserved_action_id=action.id,
            targeted_at=now_value,
        )
        session.add_all([task, memory, action, coverage])
        session.commit()

        spec = find_spec("app.services.task_center.legacy_anchor_rewrite")
        assert spec is not None
        cleanup = import_module(spec.name).expire_legacy_anchor_rewritten_actions
        expired = cleanup(session, task)
        session.flush()

        assert expired == 1
        assert action.status == "skipped"
        assert action.result["error_code"] == "voice_profile_anchor_replan"
        assert memory.status == "expired_before_send"
        assert coverage.state == "ready"
        assert coverage.reserved_action_id is None
        assert task.stats["voice_profile_anchor_replanned_open_action_count"] == 1


def test_prepare_open_actions_runs_legacy_cleanup_before_group_lookup() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        task = Task(
            id="task-prepare-legacy-anchor",
            tenant_id=1,
            name="规划前清理",
            type="group_ai_chat",
            status="running",
            type_config={"target_group_id": 999},
        )
        action = Action(
            id="action-prepare-legacy-anchor",
            tenant_id=1,
            task_id=task.id,
            task_type="group_ai_chat",
            action_type="send_message",
            account_id=11,
            status="pending",
            scheduled_at=_now(),
            payload={"group_id": 999, "message_text": "旧正文 价格咋说"},
            result={"voice_profile_anchor_rewritten": True},
        )
        session.add_all([task, action])
        session.commit()

        processed = group_ai_chat.prepare_open_actions_for_planning(session, task)

        assert processed == 1
        assert action.status == "skipped"
        assert action.result["error_code"] == "voice_profile_anchor_replan"


def test_planner_expires_generated_content_without_style_only_contract() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        task = Task(
            id="task-old-contract",
            tenant_id=1,
            name="旧面具消费者合同",
            type="group_ai_chat",
            status="running",
        )
        old_action = Action(
            id="action-old-contract",
            tenant_id=1,
            task_id=task.id,
            task_type="group_ai_chat",
            action_type="send_message",
            account_id=11,
            status="pending",
            scheduled_at=_now(),
            payload={
                "group_id": 7,
                "message_text": "这是旧 guidance 下生成但未追加固定尾句的正文",
                "ai_generation_status": "ready",
                "generation_source": "ai_provider",
            },
            result={"voice_profile_anchor_rewritten": False},
        )
        session.add_all([task, old_action])
        session.commit()

        cleanup = import_module(
            "app.services.task_center.legacy_anchor_rewrite"
        ).expire_legacy_anchor_rewritten_actions

        assert cleanup(session, task) == 1
        assert old_action.status == "skipped"
        assert old_action.result["error_code"] == "voice_profile_anchor_replan"


def test_planner_keeps_ungenerated_blueprint_and_direct_check_in() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        task = Task(
            id="task-current-contract-exemptions",
            tenant_id=1,
            name="当前合同豁免",
            type="group_ai_chat",
            status="running",
        )
        blueprint, check_in, current_provider = _contract_exemption_actions(task)
        session.add_all([task, blueprint, check_in, current_provider])
        session.commit()

        cleanup = import_module(
            "app.services.task_center.legacy_anchor_rewrite"
        ).expire_legacy_anchor_rewritten_actions

        assert cleanup(session, task) == 0
        assert blueprint.status == "pending"
        assert check_in.status == "pending"
        assert current_provider.status == "pending"


def test_dispatcher_rejects_legacy_rewrite_before_other_send_gates(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        action = Action(
            id="action-dispatch-legacy-anchor",
            tenant_id=1,
            task_id="task-dispatch-legacy-anchor",
            task_type="group_ai_chat",
            action_type="send_message",
            account_id=11,
            status="executing",
            scheduled_at=_now(),
            payload={"group_id": 7, "message_text": "旧正文 价格咋说"},
            result={"voice_profile_anchor_rewritten": True},
        )
        session.add(action)
        session.commit()
        monkeypatch.setattr(
            dispatcher,
            "_recover_pre_send_required_channel_prompt",
            lambda *_args, **_kwargs: pytest.fail("legacy rewrite must stop before send gates"),
        )

        passed = dispatcher._group_send_preconditions_pass(
            session,
            action,
            object(),
        )

        assert passed is False
        assert action.status == "skipped"
        assert action.result["error_code"] == "voice_profile_anchor_replan"


def test_dispatcher_rejects_generated_content_without_style_only_contract(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        action = Action(
            id="action-dispatch-old-contract",
            tenant_id=1,
            task_id="task-dispatch-old-contract",
            task_type="group_ai_chat",
            action_type="send_message",
            account_id=11,
            status="executing",
            scheduled_at=_now(),
            payload={
                "group_id": 7,
                "message_text": "旧 guidance 下已生成正文",
                "ai_generation_status": "ready",
                "generation_source": "ai_provider",
            },
            result={"voice_profile_anchor_rewritten": False},
        )
        session.add(action)
        session.commit()
        monkeypatch.setattr(
            dispatcher,
            "_recover_pre_send_required_channel_prompt",
            lambda *_args, **_kwargs: pytest.fail("old mask contract must stop before send gates"),
        )

        assert dispatcher._group_send_preconditions_pass(session, action, object()) is False
        assert action.status == "skipped"
        assert action.result["error_code"] == "voice_profile_anchor_replan"

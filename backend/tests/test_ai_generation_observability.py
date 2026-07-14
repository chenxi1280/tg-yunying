from __future__ import annotations

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Action, Task, Tenant, TgGroup
from app.services._common import _now
from app.services.task_center import ai_generation_dispatch
from app.services.task_center.ai_generation_pipeline import SlotGenerationResult
from app.services.task_center.ai_generation_quality import fail_generation_action
from app.services.task_center.details import _ai_generation_records
from app.services.task_center.stats import refresh_task_stats


pytestmark = pytest.mark.no_postgres


def test_generation_failure_writes_action_observability_without_updating_task() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    statements: list[str] = []
    with Session(engine) as session:
        task, action = _seed_task_action(session)
        event.listen(engine, "before_cursor_execute", _record_statements(statements))

        fail_generation_action(
            action,
            "voice_profile_mismatch",
            "账号面具要求少表情",
            stage="ai_generation_quality",
        )
        session.flush()

        assert task.stats == {}
        assert action.result["generation_stage"] == "ai_generation_quality"
        assert action.result["generation_outcome"] == "voice_profile_mismatch"
        assert action.result["generation_category"] == "quality_rejected"
        assert not any(statement.startswith("update tasks ") for statement in statements)


def test_phase_c_generation_attempt_does_not_update_task_stats_hot_row() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    statements: list[str] = []
    with Session(engine) as session:
        task, action = _seed_phase_c_action(session)
        request = _phase_c_request(task, action)
        event.listen(engine, "before_cursor_execute", _record_statements(statements))

        ai_generation_dispatch._persist_generation_results(
            session,
            request,
            [SlotGenerationResult("补上价格锚点", voice_profile_anchor_rewritten=True)],
            tokens=7,
        )
        session.flush()

        assert action.payload["ai_generation_status"] == "ready"
        assert action.result["voice_profile_anchor_rewritten"] is True
        assert task.stats == {}
        assert not any(statement.startswith("update tasks ") for statement in statements)


def test_metrics_refresh_idempotently_separates_generation_and_gateway_unknown() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        task = _seed_observability_actions(session)

        first = dict(refresh_task_stats(session, task, include_configured_accounts=False))
        second = dict(refresh_task_stats(session, task, include_configured_accounts=False))

        assert first == second
        assert second["generation_persist_unknown_count"] == 1
        assert second["unknown_after_send_count"] == 1
        assert second["gateway_unknown_count"] == 1
        assert second["generation_ready_count"] == 1
        assert second["quality_rejected_count"] == 1
        assert second["quality_rejection_counts"] == {"voice_profile_mismatch": 1}


def test_task_details_keep_generation_unknown_distinct_from_gateway_unknown() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        task = _seed_observability_actions(session)
        records = _ai_generation_records(list(session.query(Action).filter(Action.task_id == task.id)))

    statuses = {record["generation_id"]: record["status"] for record in records}
    assert statuses == {
        "generation-failed": "voice_profile_mismatch",
        "generation-gateway": "ready",
        "generation-unknown": "ai_result_persist_unknown",
    }


def _seed_task_action(session: Session) -> tuple[Task, Action]:
    session.add(Tenant(id=1, name="tenant"))
    task = Task(id="task-observable", tenant_id=1, name="task", type="group_ai_chat", status="running")
    action = Action(
        id="action-observable",
        tenant_id=1,
        task_id=task.id,
        task_type=task.type,
        action_type="send_message",
        status="executing",
        scheduled_at=_now(),
        payload={"group_id": 7, "ai_generation_id": "generation-observable", "ai_generation_status": "generating"},
    )
    session.add_all([task, action])
    session.commit()
    return task, action


def _seed_phase_c_action(session: Session) -> tuple[Task, Action]:
    task, action = _seed_task_action(session)
    session.add(TgGroup(
        id=7,
        tenant_id=1,
        tg_peer_id="-1007",
        title="生成审计群",
        auth_status="已授权运营",
        can_send=True,
        require_review=False,
    ))
    action.payload = {
        **action.payload,
        "message_text": "",
        "review_approved": True,
        "slot_id": "generation-observable:turn:1",
        "ai_generation_claim_owner": "worker-a",
        "ai_generation_claim_token": "claim-a",
        "ai_generation_attempt_id": "attempt-a",
        "ai_generation_attempt_history": [{"attempt_id": "attempt-a", "outcome": "in_progress"}],
    }
    session.commit()
    return task, action


def _phase_c_request(task: Task, action: Action):
    return type("GenerationRequest", (), {
        "tenant_id": task.tenant_id,
        "task_id": task.id,
        "group_id": 7,
        "batch_ids": [action.id],
        "claim_owner": "worker-a",
        "claim_token": "claim-a",
        "attempt_id": "attempt-a",
    })()


def _seed_observability_actions(session: Session) -> Task:
    session.add(Tenant(id=1, name="tenant"))
    task = Task(id="task-stats", tenant_id=1, name="task", type="group_ai_chat", status="running")
    session.add(task)
    session.flush()
    actions = [
        _action("unknown", "pending", "ai_result_persist_unknown", "", "generation-unknown"),
        _action("gateway", "unknown_after_send", "ready", "ready", "generation-gateway"),
        _action("failed", "failed", "voice_profile_mismatch", "voice_profile_mismatch", "generation-failed"),
    ]
    session.add_all(actions)
    session.commit()
    return task


def _action(action_id: str, status: str, generation_status: str, outcome: str, generation_id: str) -> Action:
    return Action(
        id=f"action-{action_id}",
        tenant_id=1,
        task_id="task-stats",
        task_type="group_ai_chat",
        action_type="send_message",
        status=status,
        scheduled_at=_now(),
        payload={
            "group_id": 7,
            "ai_generation_id": generation_id,
            "ai_generation_status": generation_status,
        },
        result={"generation_outcome": outcome} if outcome else {},
    )


def _record_statements(statements: list[str]):
    def record(_conn, _cursor, statement, _parameters, _context, _executemany) -> None:
        statements.append(" ".join(statement.lower().split()))

    return record

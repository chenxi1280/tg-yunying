from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Action, ExecutionAttempt, Task, Tenant, TgAccount
from app.services._common import _now
from app.services.task_center.service import list_action_attempts, list_actions_page, list_ai_cycles_page, list_message_groups_page, list_relay_batches_page


def _sqlite_session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return Session(engine)


def test_task_action_attempts_are_scoped_by_tenant_task_and_action() -> None:
    now = _now()
    with _sqlite_session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(Task(id="task-attempt", tenant_id=1, name="任务", type="group_ai_chat", status="running"))
        session.add(
            Action(
                id="action-attempt",
                tenant_id=1,
                task_id="task-attempt",
                task_type="group_ai_chat",
                action_type="send_message",
                status="failed",
                scheduled_at=now,
            )
        )
        session.add_all(
            [
                ExecutionAttempt(
                    id="attempt-2",
                    tenant_id=1,
                    action_id="action-attempt",
                    attempt_no=2,
                    status="failed",
                    failure_type="FloodWait",
                    created_at=now,
                ),
                ExecutionAttempt(
                    id="attempt-1",
                    tenant_id=1,
                    action_id="action-attempt",
                    attempt_no=1,
                    status="after_call",
                    remote_message_id="100",
                    created_at=now,
                ),
            ]
        )
        session.commit()

        rows = list_action_attempts(session, 1, "task-attempt", "action-attempt")

        assert [row.id for row in rows] == ["attempt-1", "attempt-2"]
        assert rows[1].failure_type == "FloodWait"
        with pytest.raises(ValueError, match="action not found"):
            list_action_attempts(session, 1, "other-task", "action-attempt")
        with pytest.raises(ValueError, match="action not found"):
            list_action_attempts(session, 2, "task-attempt", "action-attempt")


def test_task_actions_page_supports_planned_and_executed_status_groups() -> None:
    now = _now()
    with _sqlite_session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(Task(id="task-actions-page", tenant_id=1, name="任务", type="group_ai_chat", status="running"))
        session.add(TgAccount(id=11, tenant_id=1, display_name="计划账号", username="planned_user", phone_masked="+861***0011"))
        session.add_all(
            [
                Action(id="planned-1", tenant_id=1, task_id="task-actions-page", task_type="group_ai_chat", action_type="send_message", account_id=11, status="pending", scheduled_at=now),
                Action(id="planned-2", tenant_id=1, task_id="task-actions-page", task_type="group_ai_chat", action_type="send_message", status="executing", scheduled_at=now),
                Action(id="planned-3", tenant_id=1, task_id="task-actions-page", task_type="group_ai_chat", action_type="send_message", status="claiming", scheduled_at=now),
                Action(id="planned-4", tenant_id=1, task_id="task-actions-page", task_type="group_ai_chat", action_type="send_message", status="retryable_failed", scheduled_at=now, executed_at=now),
                Action(id="executed-1", tenant_id=1, task_id="task-actions-page", task_type="group_ai_chat", action_type="send_message", status="success", scheduled_at=now, executed_at=now),
                Action(id="executed-2", tenant_id=1, task_id="task-actions-page", task_type="group_ai_chat", action_type="send_message", status="failed", scheduled_at=now, executed_at=now),
            ]
        )
        session.commit()

        planned_rows, planned_total = list_actions_page(session, 1, "task-actions-page", status="planned", page=1, page_size=10)
        executed_rows, executed_total = list_actions_page(session, 1, "task-actions-page", status="executed", page=1, page_size=10)

    assert planned_total == 4
    assert {row["id"] for row in planned_rows} == {"planned-1", "planned-2", "planned-3", "planned-4"}
    planned_by_id = {row["id"]: row for row in planned_rows}
    assert planned_by_id["planned-1"]["account_display_name"] == "计划账号"
    assert planned_by_id["planned-1"]["account_username"] == "planned_user"
    assert executed_total == 2
    assert {row["id"] for row in executed_rows} == {"executed-1", "executed-2"}


@pytest.mark.no_postgres
def test_group_ai_actions_page_normalizes_legacy_act_type() -> None:
    now = _now()
    with _sqlite_session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(Task(id="task-actions-act-type", tenant_id=1, name="AI", type="group_ai_chat", status="running"))
        session.add(
            Action(
                id="legacy-act-type-action",
                tenant_id=1,
                task_id="task-actions-act-type",
                task_type="group_ai_chat",
                action_type="send_message",
                status="pending",
                scheduled_at=now,
                payload={"group_id": 1, "message_text": "不错", "act_type": "experience"},
            )
        )
        session.commit()

        rows, total = list_actions_page(session, 1, "task-actions-act-type", page=1, page_size=10)

    assert total == 1
    assert rows[0]["payload"]["act_type"] == "detail_follow"


def test_ai_cycle_page_returns_complete_cycle_groups() -> None:
    now = _now()
    with _sqlite_session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(Task(id="task-ai-cycles", tenant_id=1, name="AI", type="group_ai_chat", status="running"))
        session.add_all(
            [
                Action(id=f"cycle-a-{index}", tenant_id=1, task_id="task-ai-cycles", task_type="group_ai_chat", action_type="send_message", status="success", scheduled_at=now + timedelta(minutes=1), payload={"cycle_id": "cycle-a", "turn_index": index})
                for index in range(1, 4)
            ]
            + [
                Action(id="cycle-b-1", tenant_id=1, task_id="task-ai-cycles", task_type="group_ai_chat", action_type="send_message", status="success", scheduled_at=now, payload={"cycle_id": "cycle-b", "turn_index": 1})
            ]
        )
        session.commit()

        rows, total = list_ai_cycles_page(session, 1, "task-ai-cycles", page=1, page_size=1)

    assert total == 2
    assert len(rows) == 1
    assert rows[0]["cycle_id"] == "cycle-a"
    assert {turn["action_id"] for turn in rows[0]["turns"]} == {"cycle-a-1", "cycle-a-2", "cycle-a-3"}


def test_relay_batch_page_returns_complete_batch_groups() -> None:
    now = _now()
    with _sqlite_session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(Task(id="task-relay-batches", tenant_id=1, name="转发", type="group_relay", status="running"))
        session.add_all(
            [
                Action(id=f"batch-a-{index}", tenant_id=1, task_id="task-relay-batches", task_type="group_relay", action_type="send_message", status="success", scheduled_at=now + timedelta(minutes=1), payload={"relay_batch_id": "batch-a", "relay_event_id": f"a-{index}"})
                for index in range(1, 4)
            ]
            + [
                Action(id="batch-b-1", tenant_id=1, task_id="task-relay-batches", task_type="group_relay", action_type="send_message", status="success", scheduled_at=now, payload={"relay_batch_id": "batch-b", "relay_event_id": "b-1"})
            ]
        )
        session.commit()

        rows, total = list_relay_batches_page(session, 1, "task-relay-batches", page=1, page_size=1)

    assert total == 2
    assert len(rows) == 1
    assert rows[0]["relay_batch_id"] == "batch-a"
    assert {item["action_id"] for item in rows[0]["items"]} == {"batch-a-1", "batch-a-2", "batch-a-3"}


def test_message_group_page_returns_complete_message_groups() -> None:
    now = _now()
    with _sqlite_session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(Task(id="task-message-groups", tenant_id=1, name="频道", type="channel_comment", status="running", type_config={"target_comments_per_message": 3}))
        session.add_all(
            [
                Action(id=f"message-a-{index}", tenant_id=1, task_id="task-message-groups", task_type="channel_comment", action_type="post_comment", status="success", scheduled_at=now + timedelta(minutes=1), payload={"channel_id": "-1009", "channel_target_id": 21, "message_id": 9001, "message_content": "公告"})
                for index in range(1, 4)
            ]
            + [
                Action(id="message-b-1", tenant_id=1, task_id="task-message-groups", task_type="channel_comment", action_type="post_comment", status="success", scheduled_at=now, payload={"channel_id": "-1009", "channel_target_id": 21, "message_id": 9002, "message_content": "旧公告"})
            ]
        )
        session.commit()

        rows, total = list_message_groups_page(session, 1, "task-message-groups", page=1, page_size=1)

    assert total == 2
    assert len(rows) == 1
    assert rows[0]["message_id"] == 9001
    assert {action.id for action in rows[0]["actions"]} == {"message-a-1", "message-a-2", "message-a-3"}

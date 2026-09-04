from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from app.models import Action, Task, TgAccount, TgGroup
from app.services.task_center import dispatcher
from app.services.task_center.engagement_attention import apply_proactive_quiet_windows
from app.services.task_center.engagement_conversation import project_group_context_message
from app.services.task_center.executors import group_ai_chat
from tests.test_engagement_conversation import NOW, _message, session  # noqa: F401

pytestmark = pytest.mark.no_postgres


def test_direct_item_waits_for_stable_human_quiet_window(session: Session) -> None:
    task = session.get(Task, "group-task")
    group = session.get(TgGroup, 10)
    message = _message(session, 112, 512, NOW - timedelta(seconds=10), "我还在说")
    project_group_context_message(session, group, message)
    items = [{"slot_id": "direct-1"}, {"reply_target": {"message_id": 512}}]

    planned = apply_proactive_quiet_windows(
        session,
        task,
        group,
        {
            "attention_quiet_after_min_seconds": 60,
            "attention_quiet_after_max_seconds": 60,
        },
        items,
        now_value=NOW,
    )

    assert planned[0]["proactive_quiet_until_at"] == (NOW + timedelta(seconds=50)).isoformat()
    assert "proactive_quiet_until_at" not in planned[1]


def test_direct_pacing_release_is_not_before_quiet_window() -> None:
    owner = SimpleNamespace(pacing_due_at=None, release_not_before_at=None)
    assignment = SimpleNamespace(
        owner=owner,
        source_slot=SimpleNamespace(deadline_at=NOW + timedelta(hours=1)),
    )
    point = SimpleNamespace(
        due_at=NOW + timedelta(seconds=10),
        release_not_before_at=NOW + timedelta(seconds=10),
    )
    quiet_until = NOW + timedelta(seconds=50)

    timing = group_ai_chat._ai_assignment_timing(
        {"proactive_quiet_until_at": quiet_until.isoformat()},
        assignment,
        point,
    )

    assert timing == (
        point.due_at,
        quiet_until,
        assignment.source_slot.deadline_at,
        False,
    )


def test_gateway_gate_rechecks_new_human_attention(session: Session, monkeypatch) -> None:
    task = session.get(Task, "group-task")
    group = session.get(TgGroup, 10)
    account = session.get(TgAccount, 11)
    message = _message(session, 113, 513, NOW - timedelta(seconds=5), "先让我说完")
    project_group_context_message(session, group, message)
    action = Action(
        id="direct-attention-action",
        tenant_id=1,
        task_id=task.id,
        task_type=task.type,
        action_type="send_message",
        account_id=account.id,
        status="claiming",
        task_lifecycle_epoch=task.task_lifecycle_epoch,
        payload={},
    )
    session.add(action)
    session.flush()
    context = SimpleNamespace(
        group=group,
        payload=SimpleNamespace(
            reply_to_message_id=None,
            proactive_quiet_until_at=None,
        ),
    )
    monkeypatch.setattr(dispatcher, "_now", lambda: NOW)
    monkeypatch.setattr(dispatcher, "_release_runtime_resources", lambda *_args: None)

    assert not dispatcher._group_send_attention_available(session, action, context)
    assert action.status == "pending"
    assert action.scheduled_at > NOW
    assert action.result["error_code"] == "attention_quiet_after"

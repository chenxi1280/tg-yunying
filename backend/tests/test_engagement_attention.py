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


def test_attention_deadline_survives_new_events_and_reload(session: Session, monkeypatch) -> None:
    task = session.get(Task, "group-task")
    task.type_config = {**task.type_config, "attention_quiet_after_min_seconds": 180,
                        "attention_quiet_after_max_seconds": 180}
    group = session.get(TgGroup, 10)
    action = Action(id="bounded-attention", tenant_id=1, task_id=task.id,
                    task_type=task.type, action_type="send_message", account_id=11,
                    status="claiming", task_lifecycle_epoch=task.task_lifecycle_epoch, payload={})
    session.add(action)
    context = SimpleNamespace(group=group, payload=SimpleNamespace(
        reply_to_message_id=None, proactive_quiet_until_at=None))
    monkeypatch.setattr(dispatcher, "_release_runtime_resources", lambda *_args: None)
    for index, elapsed in enumerate((0, 120, 180)):
        current = NOW + timedelta(seconds=elapsed)
        monkeypatch.setattr(dispatcher, "_now", lambda: current)
        message = _message(session, 210 + index, 610 + index, current, "还有一个问题")
        project_group_context_message(session, group, message)
        allowed = dispatcher._group_send_attention_available(session, action, context)
        assert allowed is (elapsed == 180)
        assert action.result["attention_wait"]["horizon_deadline_at"] == (
            NOW + timedelta(seconds=180)).isoformat()
        session.commit()
        session.expire_all()
        action = session.get(Action, "bounded-attention")


def test_attention_can_finish_before_horizon_and_explicit_reply_bypasses(session: Session, monkeypatch) -> None:
    task = session.get(Task, "group-task")
    group = session.get(TgGroup, 10)
    action = Action(id="early-attention", tenant_id=1, task_id=task.id,
                    task_type=task.type, action_type="send_message", account_id=11,
                    status="claiming", payload={})
    context = SimpleNamespace(group=group, payload=SimpleNamespace(
        reply_to_message_id=None, proactive_quiet_until_at=None))
    monkeypatch.setattr(dispatcher, "_now", lambda: NOW)
    monkeypatch.setattr(dispatcher, "_release_runtime_resources", lambda *_args: None)
    monkeypatch.setattr("app.services.task_center.engagement_attention.latest_proactive_quiet_until",
                        lambda *_args, **_kwargs: NOW + timedelta(seconds=20))
    assert not dispatcher._group_send_attention_available(session, action, context)
    assert action.scheduled_at == NOW + timedelta(seconds=20)
    monkeypatch.setattr(dispatcher, "_now", lambda: NOW + timedelta(seconds=20))
    assert dispatcher._group_send_attention_available(session, action, context)
    context.payload.reply_to_message_id = 611
    monkeypatch.setattr(dispatcher, "_now", lambda: NOW)
    assert dispatcher._group_send_attention_available(session, action, context)

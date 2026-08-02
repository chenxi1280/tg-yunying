from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.models import Action, ExecutionAttempt, Task, TgGroup
from app.services.task_center.executors.group_ai_chat import _group_reply_target_pool
from group_ai_content_scope_test_support import _seed_scope, _session


pytestmark = pytest.mark.no_postgres


@pytest.mark.parametrize(
    "result",
    [
        {
            "error_code": "reply_target_missing",
            "validation_stage": "ai_reply_target",
            "reply_target_observation": "remote_missing_or_inaccessible",
        },
        {
            "error_code": "reply_target_missing",
            "validation_stage": "ai_reply_target",
            "error_message": "group:运营群:可访问",
        },
    ],
)
def test_remote_missing_reply_target_is_not_replanned(result: dict):
    session = _session()
    _seed_scope(session)
    _seed_reply_target(session, remote_message_id="3900003")
    session.add(_failed_reply_action(
        action_id="remote-missing-reply-use",
        remote_message_id="3900003",
        result=result,
    ))
    session.commit()

    assert _reply_target_ids(session) == []


def test_local_reply_guard_failure_does_not_poison_remote_target_pool():
    session = _session()
    _seed_scope(session)
    _seed_reply_target(session, remote_message_id="3900004")
    session.add(_failed_reply_action(
        action_id="local-guard-reply-use",
        remote_message_id="3900004",
        result={
            "error_code": "reply_target_missing",
            "error_message": "引用目标不存在或当前账号不可引用",
            "validation_stage": "ai_reply_target",
        },
    ))
    session.commit()

    assert _reply_target_ids(session) == [3900004]


def _seed_reply_target(session, *, remote_message_id: str) -> None:
    action = Action(
        id=f"prior-own-history-{remote_message_id}",
        tenant_id=1,
        task_id="task-b",
        task_type="group_ai_chat",
        action_type="send_message",
        account_id=11,
        status="success",
        payload={"group_id": 8, "message_text": "托管账号此前已发送正文"},
        executed_at=datetime.now(UTC),
    )
    session.add(action)
    session.flush()
    session.add(ExecutionAttempt(
        action_id=action.id,
        status="success",
        remote_message_id=remote_message_id,
    ))


def _failed_reply_action(
    *, action_id: str, remote_message_id: str, result: dict,
) -> Action:
    return Action(
        id=action_id,
        tenant_id=1,
        task_id="task-b",
        task_type="group_ai_chat",
        action_type="send_message",
        account_id=11,
        status="failed",
        payload={"group_id": 8, "reply_to_message_id": int(remote_message_id)},
        result=result,
    )


def _reply_target_ids(session) -> list[int]:
    targets = _group_reply_target_pool(
        session,
        session.get(Task, "task-b"),
        session.get(TgGroup, 8),
        [],
    )
    return [target["message_id"] for target in targets]

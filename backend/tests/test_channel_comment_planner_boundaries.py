import pytest
from sqlalchemy import event, select

from app.models import Action, ChannelMessage, ChannelMessageComment, Task, Tenant
from app.services.task_center.executors import channel_comment
from channel_comment_planner_test_support import (
    add_existing_comment_action,
    fixed_profile,
    forbid_planner_external_boundaries,
    planner_external_boundary_references,
    planner_session,
    seed_comment_task,
)


pytestmark = pytest.mark.no_postgres


def _state_action(
    action_id: str,
    *,
    task_id: str,
    tenant_id: int,
    status: str,
    payload: dict,
) -> Action:
    return Action(
        id=action_id,
        tenant_id=tenant_id,
        task_id=task_id,
        task_type="channel_comment",
        action_type="post_comment",
        status=status,
        payload=payload,
    )


def _batched_state_actions(task: Task) -> list[Action]:
    return [
        _state_action(
            "state-current",
            task_id=task.id,
            tenant_id=1,
            status="pending",
            payload={"channel_message_id": 41, "slot_id": "channel-comment:41:0"},
        ),
        _state_action(
            "state-legacy",
            task_id=task.id,
            tenant_id=1,
            status="failed",
            payload={"message_id": 9002, "slot_id": "channel-comment:42:5"},
        ),
        _state_action(
            "state-foreign-tenant",
            task_id=task.id,
            tenant_id=2,
            status="pending",
            payload={"channel_message_id": 44, "slot_id": "channel-comment:44:7"},
        ),
        _state_action(
            "state-other-task",
            task_id="other-comment-task",
            tenant_id=1,
            status="pending",
            payload={"message_id": 9004, "slot_id": "channel-comment:44:8"},
        ),
    ]


def _seed_batched_message_state(session, task: Task) -> set[str]:
    for database_id, message_id in ((42, 9002), (43, 9003), (44, 9004)):
        session.add(
            ChannelMessage(
                id=database_id,
                tenant_id=1,
                channel_target_id=31,
                message_id=message_id,
                content_preview=f"频道消息 {message_id}",
                comment_available=True,
            )
        )
    session.add(Tenant(id=2, name="其他租户"))
    session.add(Task(id="other-comment-task", tenant_id=1, name="其他任务", type="channel_comment", stats={}))
    actions = _batched_state_actions(task)
    session.add_all(actions)
    session.add(
        ChannelMessageComment(
            tenant_id=1,
            channel_target_id=31,
            channel_message_id=43,
            comment_message_id=8201,
            author_name="托管账号",
            author_username="comment_101",
            content_preview="已由托管账号评论",
        )
    )
    task.type_config = {**task.type_config, "message_ids": [41, 42, 43, 44], "target_comments_per_message": 1}
    session.commit()
    return {action.id for action in actions}


def _comment_state_select_count(session, callback) -> int:
    statements: list[str] = []

    def capture(_connection, _cursor, statement, _parameters, _context, _executemany):
        normalized = " ".join(statement.lower().split())
        select_clause = normalized.split(" from ", 1)[0]
        action_state = "actions.payload" in select_clause and "actions.id" not in select_clause
        managed_comments = "count(channel_message_comments.id)" in select_clause
        if action_state or managed_comments:
            statements.append(normalized)

    event.listen(session.get_bind(), "before_cursor_execute", capture)
    try:
        callback()
    finally:
        event.remove(session.get_bind(), "before_cursor_execute", capture)
    return len(statements)


@pytest.mark.parametrize("status", ["failed", "skipped"])
def test_released_comment_actions_are_replenished_with_monotonic_slots(monkeypatch, status):
    forbid_planner_external_boundaries(monkeypatch)
    fixed_profile(monkeypatch)
    with planner_session() as session:
        task = seed_comment_task(session, mode="comment")
        task.type_config = {
            **task.type_config,
            "max_total_comments": 2,
            "max_total_comments_jitter": 0,
        }

        first_created = channel_comment.build_plan(session, task)
        session.commit()
        first_actions = list(session.scalars(select(Action).where(Action.task_id == task.id)))
        first_action_ids = {action.id for action in first_actions}
        for action in first_actions:
            action.status = status
        session.commit()

        replenished = channel_comment.build_plan(session, task)
        session.commit()
        all_actions = list(session.scalars(select(Action).where(Action.task_id == task.id)))
        new_actions = [action for action in all_actions if action.id not in first_action_ids]
        capped = channel_comment.build_plan(session, task)

    assert first_created == 2
    assert replenished == 2
    assert capped == 0
    assert len(all_actions) == 4
    assert len({action.action_dedupe_key for action in all_actions}) == 4
    assert sorted(action.payload["slot_id"] for action in first_actions) == ["channel-comment:41:0", "channel-comment:41:1"]
    assert sorted(action.payload["slot_id"] for action in new_actions) == ["channel-comment:41:2", "channel-comment:41:3"]
    assert all(action.status == status for action in first_actions)
    assert all(action.status == "pending" for action in new_actions)
    assert len({action.account_id for action in new_actions}) == 2
    assert task.stats["max_total_comments_resolved"] == 2


@pytest.mark.parametrize(
    "status",
    ["pending", "claiming", "executing", "success", "unknown_after_send"],
)
def test_reserved_comment_action_holds_message_capacity_and_stable_slot(monkeypatch, status):
    forbid_planner_external_boundaries(monkeypatch)
    fixed_profile(monkeypatch)
    with planner_session() as session:
        task = seed_comment_task(session, mode="comment")
        add_existing_comment_action(session, task, status)

        created = channel_comment.build_plan(session, task)
        actions = list(
            session.scalars(
                select(Action).where(Action.task_id == task.id, Action.id != f"existing-{status}")
            )
        )

    assert created == 1
    assert [action.payload["slot_id"] for action in actions] == ["channel-comment:41:1"]


def test_planner_does_not_collect_remote_messages_for_dynamic_scope(monkeypatch):
    forbid_planner_external_boundaries(monkeypatch)
    fixed_profile(monkeypatch)
    with planner_session() as session:
        task = seed_comment_task(session, mode="comment")
        task.type_config = {
            **task.type_config,
            "message_scope": "latest_n",
            "message_ids": [],
        }

        created = channel_comment.build_plan(session, task)

    assert created == 2


def test_dynamic_scope_without_persisted_messages_waits_for_listener(monkeypatch):
    forbid_planner_external_boundaries(monkeypatch)
    fixed_profile(monkeypatch)
    with planner_session() as session:
        task = seed_comment_task(session, mode="comment")
        task.type_config = {**task.type_config, "message_scope": "latest_n", "message_ids": []}
        session.scalar(select(ChannelMessage).where(ChannelMessage.id == 41)).comment_available = False

        created = channel_comment.build_plan(session, task)

    assert created == 0
    assert task.last_error == "未找到已采集频道消息，等待监听采集"


def test_unknown_after_send_reserves_current_hour_budget(monkeypatch):
    forbid_planner_external_boundaries(monkeypatch)
    fixed_profile(monkeypatch)
    with planner_session() as session:
        task = seed_comment_task(session, mode="comment", target_count=1)
        task.pacing_config = {**task.pacing_config, "max_actions_per_hour": 1}
        task.type_config = {**task.type_config, "message_ids": [41, 42]}
        session.add(
            ChannelMessage(
                id=42,
                tenant_id=1,
                channel_target_id=31,
                message_id=9002,
                content_preview="第二条频道消息",
                comment_available=True,
            )
        )
        add_existing_comment_action(session, task, "unknown_after_send")

        created = channel_comment.build_plan(session, task)

    assert created == 0


def test_channel_comment_planner_source_has_no_external_boundary_calls():
    forbidden = {
        "ai_generator",
        "generate_channel_comments",
        "generate_channel_reply_comments",
        "generate_contents",
        "ai_gateway",
        "GrokCliBridge",
        "gateway",
        "collect_channel_messages",
        "collect_group_context",
        "fetch_channel_messages",
        "fetch_group_messages",
    }

    assert planner_external_boundary_references().isdisjoint(forbidden)


def test_planner_batches_message_state_with_legacy_and_tenant_isolation(monkeypatch):
    forbid_planner_external_boundaries(monkeypatch)
    fixed_profile(monkeypatch)
    with planner_session() as session:
        task = seed_comment_task(session, mode="comment", target_count=1)
        existing_action_ids = _seed_batched_message_state(session, task)
        created = 0

        def plan() -> None:
            nonlocal created
            created = channel_comment.build_plan(session, task)

        state_select_count = _comment_state_select_count(session, plan)
        actions = list(session.scalars(select(Action).where(Action.task_id == task.id)))
        new_actions = [action for action in actions if action.id not in existing_action_ids]

    assert state_select_count == 2
    assert created == 2
    assert sorted(action.payload["slot_id"] for action in new_actions) == [
        "channel-comment:42:6",
        "channel-comment:44:0",
    ]

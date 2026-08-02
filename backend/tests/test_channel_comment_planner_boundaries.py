from datetime import datetime

import pytest
from sqlalchemy import event, select

from app.models import (
    Action,
    ChannelMessage,
    ChannelMessageComment,
    CommentFulfillmentObligation,
    Task,
    Tenant,
    TgAccount,
)
from app.services.task_center import dispatcher
from app.services.task_center.executors import channel_comment, channel_comment_budget
from app.services.task_center.fulfillment_retry import retry_failed_actions
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


def _batched_managed_comments() -> list[ChannelMessageComment]:
    return [
        ChannelMessageComment(
            tenant_id=1,
            channel_target_id=31,
            channel_message_id=43,
            comment_message_id=8201,
            author_name="托管账号",
            author_username="comment_101",
            content_preview="已由托管账号评论",
        ),
        ChannelMessageComment(
            tenant_id=1,
            channel_target_id=31,
            channel_message_id=44,
            comment_message_id=8202,
            author_name="外租户同名账号",
            author_username="foreign_user",
            content_preview="不应计入本租户托管评论",
        ),
        ChannelMessageComment(
            tenant_id=1,
            channel_target_id=31,
            channel_message_id=44,
            comment_message_id=8203,
            author_name="空账号",
            author_username=None,
            content_preview="空 username 不应计入",
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
    session.add(
        TgAccount(
            id=201,
            tenant_id=2,
            display_name="外租户账号",
            username="FOREIGN_USER",
            phone_masked="201",
            status="active",
        )
    )
    session.add(Task(id="other-comment-task", tenant_id=1, name="其他任务", type="channel_comment", stats={}))
    actions = _batched_state_actions(task)
    session.add_all(actions)
    session.flush()
    session.add_all(
        [
            CommentFulfillmentObligation(
                tenant_id=1,
                task_id=task.id,
                channel_message_id=41,
                comment_plan_revision=1,
                target_ordinal=1,
                current_action_id="state-current",
                action_attempt_no=1,
                status="pending",
            ),
            CommentFulfillmentObligation(
                tenant_id=1,
                task_id=task.id,
                channel_message_id=42,
                comment_plan_revision=1,
                target_ordinal=6,
                action_attempt_no=1,
                status="replan_required",
            ),
        ]
    )
    session.add_all(_batched_managed_comments())
    session.get(TgAccount, 101).username = " @COMMENT_101 "
    task.type_config = {**task.type_config, "message_ids": [41, 42, 43, 44], "target_comments_per_message": 1}
    session.commit()
    return {action.id for action in actions}


def _select_count(session, callback) -> int:
    statements: list[str] = []

    def capture(_connection, _cursor, statement, _parameters, _context, _executemany):
        normalized = " ".join(statement.lower().split())
        if normalized.startswith("select "):
            statements.append(normalized)

    event.listen(session.get_bind(), "before_cursor_execute", capture)
    try:
        callback()
    finally:
        event.remove(session.get_bind(), "before_cursor_execute", capture)
    return len(statements)


def test_reply_replan_refreshes_missing_target_for_same_ordinal(monkeypatch):
    forbid_planner_external_boundaries(monkeypatch)
    fixed_profile(monkeypatch)
    with planner_session() as session:
        task = seed_comment_task(
            session,
            mode="mixed",
            reply_min=1,
            target_count=1,
        )
        assert channel_comment.build_plan(session, task) == 1
        first = session.scalar(select(Action).where(Action.task_id == task.id))
        obligation_id = first.payload["comment_fulfillment_obligation_id"]
        stale_target = first.payload["reply_to_message_id"]
        stale = session.scalar(select(ChannelMessageComment).where(
            ChannelMessageComment.comment_message_id == stale_target,
        ))
        session.delete(stale)
        first.status = "failed"
        dispatcher._sync_comment_fulfillment_state(session, first)
        session.commit()

        assert channel_comment.build_plan(session, task) == 1
        replacements = list(session.scalars(
            select(Action).where(
                Action.task_id == task.id,
                Action.id != first.id,
            )
        ))

    assert len(replacements) == 1
    assert replacements[0].payload["comment_fulfillment_obligation_id"] == obligation_id
    assert replacements[0].payload["reply_to_message_id"] == 8102
    assert replacements[0].payload["comment_action_attempt_no"] == 2


def test_reply_replan_waits_when_only_direct_target_is_available(monkeypatch):
    forbid_planner_external_boundaries(monkeypatch)
    fixed_profile(monkeypatch)
    with planner_session() as session:
        task = seed_comment_task(
            session,
            mode="mixed",
            reply_min=1,
            target_count=1,
        )
        assert channel_comment.build_plan(session, task) == 1
        first = session.scalar(select(Action).where(Action.task_id == task.id))
        obligation_id = first.payload["comment_fulfillment_obligation_id"]
        for comment in session.scalars(select(ChannelMessageComment)):
            session.delete(comment)
        first.status = "failed"
        dispatcher._sync_comment_fulfillment_state(session, first)
        session.commit()

        assert channel_comment.build_plan(session, task) == 0
        obligation = session.get(CommentFulfillmentObligation, obligation_id)
        actions = list(session.scalars(select(Action).where(Action.task_id == task.id)))

    assert len(actions) == 1
    assert obligation.relation_kind == "reply"
    assert obligation.status == "replan_required"
    assert obligation.current_action_id is None


def test_comment_replan_claims_only_current_hour_budget(monkeypatch):
    forbid_planner_external_boundaries(monkeypatch)
    fixed_profile(monkeypatch)
    with planner_session() as session:
        task = seed_comment_task(session, mode="comment", target_count=3)
        assert channel_comment.build_plan(session, task) == 3
        first_actions = list(session.scalars(select(Action).where(Action.task_id == task.id)))
        for action in first_actions:
            action.status = "failed"
            dispatcher._sync_comment_fulfillment_state(session, action)
        task.pacing_config = {**task.pacing_config, "max_actions_per_hour": 1}
        session.commit()

        assert channel_comment.build_plan(session, task) == 1
        replacements = list(session.scalars(select(Action).where(
            Action.task_id == task.id,
            Action.id.not_in([action.id for action in first_actions]),
        )))

    assert len(replacements) == 1


def test_released_comment_action_is_replanned_instead_of_retried(monkeypatch):
    forbid_planner_external_boundaries(monkeypatch)
    fixed_profile(monkeypatch)
    with planner_session() as session:
        task = seed_comment_task(session, mode="comment", target_count=1)
        task.failure_policy = {"max_retries": 3, "retry_delay_seconds": 30}
        assert channel_comment.build_plan(session, task) == 1
        action = session.scalar(select(Action).where(Action.task_id == task.id))
        action.status = "failed"
        dispatcher._sync_comment_fulfillment_state(session, action)
        session.commit()

        retried = retry_failed_actions(
            session,
            task,
            now_value=datetime(2026, 8, 2, 10, 0, 0),
        )

    assert retried == 0
    assert action.status == "failed"
    assert action.retry_count == 0


@pytest.mark.parametrize("status", ["cancelled", "failed", "skipped"])
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
    assert sorted(action.payload["slot_id"] for action in new_actions) == ["channel-comment:41:0", "channel-comment:41:1"]
    assert {action.payload["comment_action_attempt_no"] for action in first_actions} == {1}
    assert {action.payload["comment_action_attempt_no"] for action in new_actions} == {2}
    assert {
        action.payload["comment_fulfillment_obligation_id"]
        for action in first_actions
    } == {
        action.payload["comment_fulfillment_obligation_id"]
        for action in new_actions
    }
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
        messages = list(
            session.scalars(
                select(ChannelMessage).where(ChannelMessage.id.in_([41, 42, 43, 44])).order_by(ChannelMessage.id)
            )
        )
        _ = (task.id, task.tenant_id)
        states = {}

        def load_states() -> None:
            nonlocal states
            states = channel_comment_budget.load_message_comment_plan_states(session, task, messages)

        state_select_count = _select_count(session, load_states)
        created = channel_comment.build_plan(session, task)
        actions = list(session.scalars(select(Action).where(Action.task_id == task.id)))
        new_actions = [action for action in actions if action.id not in existing_action_ids]

    assert state_select_count == 2
    assert (states[41].reservation_count, states[41].next_slot_index, states[41].managed_collected_count) == (1, 1, 0)
    assert (states[42].reservation_count, states[42].next_slot_index, states[42].managed_collected_count) == (0, 6, 0)
    assert (states[43].reservation_count, states[43].next_slot_index, states[43].managed_collected_count) == (0, 0, 1)
    assert (states[44].reservation_count, states[44].next_slot_index, states[44].managed_collected_count) == (0, 0, 0)
    assert created == 2
    assert sorted(action.payload["slot_id"] for action in new_actions) == [
        "channel-comment:42:5",
        "channel-comment:44:0",
    ]

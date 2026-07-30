from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    Action,
    ChannelMessage,
    CommentFulfillmentObligation,
    OperationTarget,
    Task,
    Tenant,
)
from app.services.task_center import dispatcher


pytestmark = pytest.mark.no_postgres


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as current:
        current.add(Tenant(id=1, name="单用户"))
        current.commit()
        yield current


def test_superseded_comment_action_does_not_block_task(
    session: Session,
) -> None:
    channel, message, task = _comment_scope(
        channel_id=44,
        message_id=54,
        task_id="superseded-comment-task",
    )
    current = _comment_action(
        "current-comment-action",
        task,
        message,
        status="pending",
    )
    obligation = _obligation(
        "superseded-comment-obligation",
        task,
        message,
        current_action_id=current.id,
        attempt_no=2,
    )
    stale = _comment_action(
        "stale-comment-action",
        task,
        message,
        status="executing",
        obligation_id=obligation.id,
    )
    session.add_all([channel, message, task, current, obligation, stale])
    session.flush()

    accepted = dispatcher._ensure_comment_fulfillment_contract(session, stale)

    assert accepted is False
    assert stale.status == "skipped"
    assert stale.result["error_code"] == "comment_obligation_superseded"
    assert obligation.current_action_id == current.id
    assert obligation.status == "pending"
    assert "fulfillment_takeover_status" not in task.stats


def test_cancelled_comment_action_allows_replacement_binding(
    session: Session,
) -> None:
    channel, message, task = _comment_scope(
        channel_id=47,
        message_id=57,
        task_id="cancelled-comment-task",
    )
    cancelled = _comment_action(
        "cancelled-comment-action",
        task,
        message,
        status="cancelled",
    )
    obligation = _obligation(
        "cancelled-comment-obligation",
        task,
        message,
        current_action_id=cancelled.id,
        attempt_no=1,
    )
    replacement = _comment_action(
        "replacement-comment-action",
        task,
        message,
        status="executing",
        obligation_id=obligation.id,
    )
    session.add_all([channel, message, task, cancelled, obligation, replacement])
    session.flush()

    accepted = dispatcher._ensure_comment_fulfillment_contract(
        session,
        replacement,
    )

    assert accepted is True
    assert replacement.status == "executing"
    assert obligation.current_action_id == replacement.id
    assert obligation.action_attempt_no == 2
    assert obligation.status == "pending"
    assert "fulfillment_takeover_status" not in task.stats


def test_cancelled_comment_action_releases_current_obligation(
    session: Session,
) -> None:
    channel, message, task = _comment_scope(
        channel_id=48,
        message_id=58,
        task_id="cancelled-comment-sync-task",
    )
    action = _comment_action(
        "cancelled-comment-sync-action",
        task,
        message,
        status="cancelled",
        obligation_id="cancelled-comment-sync-obligation",
    )
    obligation = _obligation(
        "cancelled-comment-sync-obligation",
        task,
        message,
        current_action_id=action.id,
        attempt_no=1,
    )
    session.add_all([channel, message, task, action, obligation])
    session.flush()

    dispatcher._sync_comment_fulfillment_state(session, action)

    assert obligation.status == "replan_required"
    assert obligation.current_action_id is None


def test_stale_reply_comment_is_replanned_before_payload_validation(
    session: Session,
) -> None:
    channel, message, task = _comment_scope(
        channel_id=45,
        message_id=55,
        task_id="stale-reply-comment-task",
    )
    action = _comment_action(
        "stale-reply-comment-action",
        task,
        message,
        status="executing",
        obligation_id="stale-reply-comment-obligation",
        generation_status="reply_target_stale",
    )
    obligation = _obligation(
        "stale-reply-comment-obligation",
        task,
        message,
        current_action_id=action.id,
        attempt_no=1,
        relation_kind="reply",
    )
    session.add_all([channel, message, task, action, obligation])
    session.flush()

    accepted = dispatcher._ensure_comment_fulfillment_contract(
        session,
        action,
    )

    assert accepted is False
    assert action.status == "skipped"
    assert action.result["error_code"] == "reply_target_stale"
    assert "fulfillment_takeover_status" not in task.stats


def test_confirmed_comment_obligation_rejects_stale_action_without_blocking(
    session: Session,
) -> None:
    channel, message, task = _comment_scope(
        channel_id=46,
        message_id=56,
        task_id="confirmed-comment-task",
    )
    current = _comment_action(
        "confirmed-comment-action",
        task,
        message,
        status="success",
    )
    obligation = _obligation(
        "confirmed-comment-obligation",
        task,
        message,
        current_action_id=current.id,
        attempt_no=1,
        status="confirmed",
    )
    stale = _comment_action(
        "confirmed-stale-comment-action",
        task,
        message,
        status="executing",
        obligation_id=obligation.id,
    )
    session.add_all([channel, message, task, current, obligation, stale])
    session.flush()

    accepted = dispatcher._ensure_comment_fulfillment_contract(session, stale)

    assert accepted is False
    assert stale.status == "skipped"
    assert stale.result["error_code"] == "remote_fact_already_fulfilled"
    assert obligation.current_action_id == current.id
    assert obligation.status == "confirmed"
    assert "fulfillment_takeover_status" not in task.stats


def _comment_scope(
    *,
    channel_id: int,
    message_id: int,
    task_id: str,
) -> tuple[OperationTarget, ChannelMessage, Task]:
    channel = OperationTarget(
        id=channel_id,
        tenant_id=1,
        target_type="channel",
        tg_peer_id=f"-100{channel_id}",
        title="评论履约频道",
    )
    message = ChannelMessage(
        id=message_id,
        tenant_id=1,
        channel_target_id=channel.id,
        message_id=900 + message_id,
    )
    task = Task(
        id=task_id,
        tenant_id=1,
        name="评论履约",
        type="channel_comment",
        status="running",
    )
    return channel, message, task


def _comment_action(
    action_id: str,
    task: Task,
    message: ChannelMessage,
    *,
    status: str,
    obligation_id: str = "",
    generation_status: str = "",
) -> Action:
    payload = {"channel_message_id": message.id}
    if obligation_id:
        payload["comment_fulfillment_obligation_id"] = obligation_id
    if generation_status:
        payload["ai_generation_status"] = generation_status
    return Action(
        id=action_id,
        tenant_id=1,
        task_id=task.id,
        task_type=task.type,
        action_type="post_comment",
        status=status,
        payload=payload,
    )


def _obligation(
    obligation_id: str,
    task: Task,
    message: ChannelMessage,
    *,
    current_action_id: str,
    attempt_no: int,
    relation_kind: str = "direct",
    status: str = "pending",
) -> CommentFulfillmentObligation:
    return CommentFulfillmentObligation(
        id=obligation_id,
        tenant_id=1,
        task_id=task.id,
        channel_message_id=message.id,
        comment_plan_revision=1,
        target_ordinal=1,
        relation_kind=relation_kind,
        current_action_id=current_action_id,
        action_attempt_no=attempt_no,
        status=status,
    )

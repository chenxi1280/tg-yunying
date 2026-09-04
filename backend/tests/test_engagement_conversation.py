from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    Action,
    ContextTurn,
    ConversationEvent,
    ConversationTurnClaim,
    GroupContextMessage,
    InteractionOpportunity,
    StageWakeOutbox,
    Task,
    TaskPlannerWakeState,
    Tenant,
    TgAccount,
    TgGroup,
)
from app.services.task_center.engagement_conversation import (
    bind_conversation_turn_claim,
    interaction_reply_targets,
    project_group_context_message,
    settle_conversation_turn_claim,
    validate_conversation_turn_claim_for_gateway,
)
from app.services.task_center.engagement_conversation_wake import (
    drain_due_conversation_wakes,
)
from app.services.task_center.engagement_conversation_remote import (
    validate_remote_conversation_context,
)
from app.services.task_center.engagement_target_scope import ensure_task_target_scope_claims
from app.services.task_center import dispatcher
from app.services.task_center.executors import group_ai_chat


pytestmark = pytest.mark.no_postgres
NOW = datetime(2026, 9, 4, 12, 0, 10)


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as current:
        current.add_all([
            Tenant(id=1, name="tenant"),
            TgAccount(
                id=11,
                tenant_id=1,
                display_name="account",
                phone_masked="***11",
                status="在线",
            ),
            TgGroup(
                id=10,
                tenant_id=1,
                tg_peer_id="-10010",
                title="group",
            ),
            Task(
                id="group-task",
                tenant_id=1,
                name="group task",
                type="group_ai_chat",
                status="running",
                task_lifecycle_epoch=2,
                fulfillment_contract_version="fact_first_v3",
                type_config={
                    "engagement_contract_version": "unified_engagement_v1",
                    "target_group_id": 10,
                },
            ),
        ])
        current.commit()
        yield current


def _message(
    session: Session,
    message_id: int,
    remote_id: int,
    sent_at: datetime,
    content: str,
) -> GroupContextMessage:
    row = GroupContextMessage(
        id=message_id,
        tenant_id=1,
        group_id=10,
        listener_account_id=11,
        sender_peer_id="human-1",
        sender_name="真人用户",
        content=content,
        remote_message_id=str(remote_id),
        sent_at=sent_at,
    )
    session.add(row)
    session.flush()
    return row


def _bound_reply_action(
    session: Session,
    *,
    local_id: int,
    remote_id: int,
    action_id: str,
    content: str,
) -> tuple[Action, dict]:
    task = session.get(Task, "group-task")
    group = session.get(TgGroup, 10)
    ensure_task_target_scope_claims(session, task)
    message = _message(
        session,
        local_id,
        remote_id,
        NOW - timedelta(seconds=7),
        content,
    )
    target = interaction_reply_targets(session, task, group, context_rows=[message], now_value=NOW)[0]
    action = Action(
        id=action_id,
        tenant_id=1,
        task_id=task.id,
        task_type=task.type,
        action_type="send_message",
        account_id=11,
        status="claiming",
        task_lifecycle_epoch=task.task_lifecycle_epoch,
        payload={
            "reply_to_message_id": target["message_id"],
            "conversation_turn_claim_id": target["conversation_turn_claim_id"],
            "context_turn_revision": target["context_turn_revision"],
            "freshness_deadline_at": target["freshness_deadline_at"],
        },
    )
    session.add(action)
    session.flush()
    bind_conversation_turn_claim(session, action)
    return action, target


def test_human_burst_becomes_one_claimed_turn(session: Session) -> None:
    task = session.get(Task, "group-task")
    group = session.get(TgGroup, 10)
    ensure_task_target_scope_claims(session, task)
    first = _message(session, 101, 501, NOW - timedelta(seconds=10), "这个活动")
    second = _message(session, 102, 502, NOW - timedelta(seconds=7), "几点开始")

    project_group_context_message(session, group, first)
    project_group_context_message(session, group, second)
    targets = interaction_reply_targets(session, task, group, context_rows=[first, second], now_value=NOW)

    assert len(targets) == 1
    assert targets[0]["message_id"] == 502
    assert targets[0]["preview"] == "几点开始"
    assert targets[0]["content"] == "真人用户: 这个活动\n真人用户: 几点开始"
    assert session.scalar(select(func.count(ContextTurn.id))) == 1
    turn = session.scalar(select(ContextTurn))
    assert turn.event_count == 2
    assert turn.state == "closed"
    assert session.scalar(select(func.count(ConversationTurnClaim.id))) == 1
    assert session.scalar(select(func.count(StageWakeOutbox.id))) == 2
    assert session.scalar(select(func.count(StageWakeOutbox.id)).where(
        StageWakeOutbox.state == "delivered"
    )) == 1
    assert session.scalar(select(func.count(StageWakeOutbox.id)).where(
        StageWakeOutbox.state == "superseded"
    )) == 1


def test_turn_claim_binds_one_action_and_pre_call_revision(session: Session) -> None:
    task = session.get(Task, "group-task")
    group = session.get(TgGroup, 10)
    ensure_task_target_scope_claims(session, task)
    message = _message(session, 103, 503, NOW - timedelta(seconds=7), "现在方便聊吗")
    target = interaction_reply_targets(session, task, group, context_rows=[message], now_value=NOW)[0]
    action = Action(
        id="reply-action",
        tenant_id=1,
        task_id=task.id,
        task_type=task.type,
        action_type="send_message",
        account_id=11,
        status="pending",
        task_lifecycle_epoch=task.task_lifecycle_epoch,
        payload={
            "reply_to_message_id": target["message_id"],
            "conversation_turn_claim_id": target["conversation_turn_claim_id"],
            "context_turn_revision": target["context_turn_revision"],
        },
    )
    session.add(action)
    session.flush()

    bind_conversation_turn_claim(session, action)
    assert validate_conversation_turn_claim_for_gateway(
        session, action, now_value=NOW
    ) == (True, "")

    turn = session.get(ContextTurn, target["context_turn_id"])
    turn.version += 1
    allowed, reason = validate_conversation_turn_claim_for_gateway(
        session, action, now_value=NOW
    )
    assert not allowed
    assert reason == "context_turn_revision_stale"
    claim = session.get(ConversationTurnClaim, target["conversation_turn_claim_id"])
    assert claim.state == "stale"


def test_success_settles_claim_and_opportunity(session: Session) -> None:
    task = session.get(Task, "group-task")
    group = session.get(TgGroup, 10)
    ensure_task_target_scope_claims(session, task)
    message = _message(session, 104, 504, NOW - timedelta(seconds=7), "有人知道吗")
    target = interaction_reply_targets(session, task, group, context_rows=[message], now_value=NOW)[0]
    action = Action(
        id="served-action",
        tenant_id=1,
        task_id=task.id,
        task_type=task.type,
        action_type="send_message",
        account_id=11,
        status="success",
        task_lifecycle_epoch=task.task_lifecycle_epoch,
        payload={"conversation_turn_claim_id": target["conversation_turn_claim_id"]},
    )
    session.add(action)
    session.flush()
    bind_conversation_turn_claim(session, action)

    settle_conversation_turn_claim(session, action, outcome="served")

    claim = session.get(ConversationTurnClaim, target["conversation_turn_claim_id"])
    opportunity = session.get(InteractionOpportunity, target["interaction_opportunity_id"])
    assert claim.state == "served"
    assert opportunity.state == "served"


def test_unfrozen_quantity_slot_reflows_into_response_window(monkeypatch) -> None:
    monkeypatch.setattr(group_ai_chat, "_now", lambda: NOW)
    owner = SimpleNamespace(pacing_due_at=None, release_not_before_at=None)
    assignment = SimpleNamespace(
        owner=owner,
        source_slot=SimpleNamespace(deadline_at=NOW + timedelta(hours=8)),
    )
    point = SimpleNamespace(
        due_at=NOW + timedelta(hours=2),
        release_not_before_at=NOW + timedelta(hours=2),
    )
    item = {
        "reply_target": {
            "conversation_turn_claim_id": "claim",
            "response_not_before_at": (NOW - timedelta(seconds=2)).isoformat(),
            "freshness_deadline_at": (NOW + timedelta(seconds=80)).isoformat(),
        }
    }

    timing = group_ai_chat._ai_assignment_timing(item, assignment, point)

    assert timing == (
        NOW,
        NOW,
        NOW + timedelta(seconds=80),
        True,
    )


def test_due_turn_wake_materializes_claim_and_wakes_planner(session: Session) -> None:
    task = session.get(Task, "group-task")
    group = session.get(TgGroup, 10)
    ensure_task_target_scope_claims(session, task)
    message = _message(session, 105, 505, NOW - timedelta(seconds=7), "还有人吗")
    project_group_context_message(session, group, message)

    assert drain_due_conversation_wakes(session, now_value=NOW) == 1

    turn = session.scalar(select(ContextTurn))
    wake = session.scalar(select(StageWakeOutbox))
    planner_wake = session.scalar(select(TaskPlannerWakeState))
    assert turn.state == "closed"
    assert wake.state == "delivered"
    assert session.scalar(select(func.count(InteractionOpportunity.id))) == 1
    assert session.scalar(select(func.count(ConversationTurnClaim.id))) == 1
    assert planner_wake.task_id == task.id
    assert planner_wake.reason_code == "conversation_turn_ready"
    assert planner_wake.not_before_at == NOW


def test_old_turn_revision_wake_cannot_close_extended_turn(session: Session) -> None:
    group = session.get(TgGroup, 10)
    first = _message(session, 106, 506, NOW - timedelta(seconds=5), "等下")
    second = _message(session, 107, 507, NOW - timedelta(seconds=2), "我补充一下")
    project_group_context_message(session, group, first)
    project_group_context_message(session, group, second)

    assert drain_due_conversation_wakes(session, now_value=NOW) == 1

    turn = session.scalar(select(ContextTurn))
    wakes = list(session.scalars(select(StageWakeOutbox).order_by(
        StageWakeOutbox.aggregate_revision
    )))
    assert turn.state == "assembling"
    assert [wake.state for wake in wakes] == ["superseded", "pending"]
    assert session.scalar(select(func.count(InteractionOpportunity.id))) == 0


def test_ownerless_wake_retries_only_inside_freshness_window(session: Session) -> None:
    group = session.get(TgGroup, 10)
    message = _message(session, 108, 508, NOW - timedelta(seconds=7), "在吗")
    project_group_context_message(session, group, message)

    assert drain_due_conversation_wakes(session, now_value=NOW) == 0
    wake = session.scalar(select(StageWakeOutbox))
    assert wake.state == "pending"
    assert wake.available_at == NOW + timedelta(seconds=2)

    expired_at = NOW + timedelta(seconds=89)
    assert drain_due_conversation_wakes(session, now_value=expired_at) == 1
    assert wake.state == "expired"


def test_remote_parent_edit_invalidates_bound_turn(session: Session) -> None:
    action, target = _bound_reply_action(
        session,
        local_id=109,
        remote_id=509,
        action_id="remote-edit-action",
        content="原来的问题",
    )
    snapshots = [SimpleNamespace(remote_message_id="509", content="已经修改的问题")]

    decision = validate_remote_conversation_context(session, action, snapshots)

    assert not decision.allowed
    assert decision.reason == "reply_parent_remote_edited"
    event = session.get(ConversationEvent, target["conversation_event_id"])
    claim = session.get(ConversationTurnClaim, target["conversation_turn_claim_id"])
    assert event.is_current is False
    assert claim.state == "stale"


def test_remote_topic_overtake_rejects_late_reply(session: Session) -> None:
    action, target = _bound_reply_action(
        session,
        local_id=110,
        remote_id=510,
        action_id="remote-overtake-action",
        content="这个怎么参加",
    )
    snapshots = [
        SimpleNamespace(remote_message_id=str(remote_id), content="新消息")
        for remote_id in range(516, 510, -1)
    ]
    snapshots.append(SimpleNamespace(remote_message_id="510", content="这个怎么参加"))

    decision = validate_remote_conversation_context(session, action, snapshots)

    assert not decision.allowed
    assert decision.reason == "context_remote_topic_overtaken"
    claim = session.get(ConversationTurnClaim, target["conversation_turn_claim_id"])
    assert claim.state == "stale"


def test_remote_probe_failure_defers_same_claim(session: Session, monkeypatch) -> None:
    action, target = _bound_reply_action(
        session,
        local_id=111,
        remote_id=511,
        action_id="remote-probe-action",
        content="还在吗",
    )
    task = session.get(Task, "group-task")
    group = session.get(TgGroup, 10)
    account = session.get(TgAccount, 11)
    payload = SimpleNamespace(
        conversation_turn_claim_id=target["conversation_turn_claim_id"],
        freshness_deadline_at=datetime.fromisoformat(target["freshness_deadline_at"]),
    )
    context = SimpleNamespace(
        account=account,
        credentials=object(),
        group=group,
        payload=payload,
    )
    monkeypatch.setattr(
        dispatcher.gateway,
        "fetch_group_messages",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError("probe timeout")),
    )
    monkeypatch.setattr(dispatcher, "_release_runtime_resources", lambda *_args: None)
    attempt = SimpleNamespace(
        result_snapshot={"telegram_connect_timeout_seconds": 5},
    )

    assert not dispatcher._conversation_remote_context_current(
        session,
        action,
        context,
        attempt,
    )

    claim = session.get(ConversationTurnClaim, target["conversation_turn_claim_id"])
    assert action.status == "pending"
    assert action.result["error_code"] == "conversation_remote_probe_failed"
    assert action.result["validation_stage"] == "conversation_remote_context"
    assert claim.state == "bound"
    assert task.status == "running"

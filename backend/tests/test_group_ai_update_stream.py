from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    AccountBehaviorBudgetLedger,
    AccountBehaviorBudgetPolicyRevision,
    AccountExternalUseHold,
    Action,
    ContextTurn,
    ConversationEvent,
    ConversationTurnClaim,
    ExternalAccountUsePolicyRevision,
    GroupContextMessage,
    StageWakeOutbox,
    Task,
    TelegramAuthorizationUpdateDelivery,
    TelegramAuthorizationUpdateState,
    TelegramAuthorizationUpdateSubscription,
    Tenant,
    TgAccount,
    TgAccountAuthorization,
    TgGroup,
    UnownedOutboundActivityObservation,
)
from app.services._common import _now
from app.services.task_center.group_ai_update_stream import (
    consume_group_ai_update_deliveries,
    ensure_group_ai_update_subscription,
)
from app.services.task_center.engagement_conversation import materialize_due_turns
from app.services.task_center.telegram_update_ingress import (
    NormalizedUpdateIngress,
    ingest_normalized_update,
)


pytestmark = pytest.mark.no_postgres


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return Session(engine)


def _seed(session: Session) -> tuple[Task, TgGroup, TelegramAuthorizationUpdateState]:
    session.add(Tenant(id=1, name="测试租户"))
    account = TgAccount(
        id=11,
        tenant_id=1,
        display_name="监听账号",
        phone_masked="11",
        status="在线",
        authorization_generation=1,
    )
    authorization = TgAccountAuthorization(
        id=21,
        tenant_id=1,
        account_id=11,
        slot_generation=1,
        is_current=True,
        is_slot_current=True,
        status="active",
        session_ciphertext="encrypted-session",
    )
    group = TgGroup(
        id=7,
        tenant_id=1,
        tg_peer_id="-1007",
        title="活跃群",
        auth_status="已授权运营",
    )
    task = Task(
        id="group-ai-stream",
        tenant_id=1,
        name="实时活群",
        type="group_ai_chat",
        status="running",
        type_config={"engagement_contract_version": "unified_engagement_v1"},
    )
    session.add_all([
        account,
        authorization,
        group,
        task,
        AccountBehaviorBudgetPolicyRevision(
            tenant_id=1,
            account_class="normal",
            action_budgets={"total": 20, "authored_message": 10},
        ),
        ExternalAccountUsePolicyRevision(
            tenant_id=1,
            hold_seconds_by_class={"authored_message": 600},
            collision_classes_by_class={
                "authored_message": ["authored_message", "reaction"],
            },
        ),
    ])
    session.flush()
    account.current_authorization_id = authorization.id
    assert ensure_group_ai_update_subscription(
        session, task, group, listener_account_id=account.id,
    )
    state = session.scalar(select(TelegramAuthorizationUpdateState))
    state.state = "live"
    state.owner_id = "collector-test"
    state.lease_expires_at = _now() + timedelta(minutes=1)
    session.flush()
    return task, group, state


def _ingest(
    session: Session,
    state: TelegramAuthorizationUpdateState,
    *,
    remote_id: int,
    content: str,
    sender_is_bot: bool = False,
    event_type: str = "message_new",
) -> None:
    ingest_normalized_update(
        session,
        state.id,
        NormalizedUpdateIngress(
            update_identity_key=f"{event_type}:{remote_id}:{content}",
            constructor_name="UpdateNewChannelMessage",
            pts_evidence=remote_id,
            pts_count_evidence=1,
            routing_peer_type="channel",
            routing_peer_id="-1007",
            normalized_items=({
                "source_message_id": remote_id,
                "event_type": event_type,
                "sender_peer_type": "user",
                "sender_peer_id": f"user-{remote_id}",
                "sender_name": "真人用户",
                "sender_is_bot": sender_is_bot,
                "media_type": "text",
                "content": content,
                "sent_at": datetime(2026, 9, 5, 10, 0).isoformat(),
            },),
        ),
        owner_id="collector-test",
        owner_fencing_epoch=state.owner_fencing_epoch,
    )


def test_update_delivery_enters_conversation_pipeline_without_group_poll() -> None:
    with _session() as session:
        task, group, state = _seed(session)
        _ingest(session, state, remote_id=101, content="有人在吗？")

        assert consume_group_ai_update_deliveries(session, task, group) == 1
        context = session.scalar(select(GroupContextMessage))
        event = session.scalar(select(ConversationEvent))
        turn = session.scalar(select(ContextTurn))
        wake = session.scalar(select(StageWakeOutbox))
        delivery = session.scalar(select(TelegramAuthorizationUpdateDelivery))
        subscription = session.scalar(select(TelegramAuthorizationUpdateSubscription))

        assert context is not None and context.remote_message_id == "101"
        assert event is not None and event.source_context_message_id == context.id
        assert turn is not None and turn.event_ids == [event.id]
        assert wake is not None and wake.available_at == turn.closed_at
        assert delivery.delivery_state == "consumed"
        assert subscription.state == "active"


def test_stream_bot_message_is_context_only_not_human_turn() -> None:
    with _session() as session:
        task, group, state = _seed(session)
        _ingest(
            session,
            state,
            remote_id=102,
            content="服务通知",
            sender_is_bot=True,
        )

        assert consume_group_ai_update_deliveries(session, task, group) == 1
        assert session.scalar(select(func.count(GroupContextMessage.id))) == 1
        assert session.scalar(select(func.count(ConversationEvent.id))) == 0


def test_managed_unowned_group_message_is_held_not_treated_as_human() -> None:
    with _session() as session:
        task, group, state = _seed(session)
        authorization = session.get(TgAccountAuthorization, 21)
        authorization.telegram_user_id_digest = hashlib.sha256(b"user-109").hexdigest()
        _ingest(session, state, remote_id=109, content="人工客户端发言")

        assert consume_group_ai_update_deliveries(session, task, group) == 0
        assert session.scalar(select(func.count(GroupContextMessage.id))) == 0
        assert session.scalar(select(UnownedOutboundActivityObservation)) is not None
        assert session.scalar(select(AccountExternalUseHold)) is not None
        ledger = session.scalar(select(AccountBehaviorBudgetLedger))
        assert ledger.counters["authored_message"]["unowned"] == 1


def test_stream_ignores_edit_without_invalidating_or_creating_another_reply() -> None:
    with _session() as session:
        task, group, state = _seed(session)
        _ingest(session, state, remote_id=103, content="原问题")
        consume_group_ai_update_deliveries(session, task, group)
        materialize_due_turns(
            session,
            task,
            group,
            now_value=datetime(2026, 9, 5, 10, 0, 6),
        )
        claim = session.scalar(select(ConversationTurnClaim))
        turn = session.get(ContextTurn, claim.context_turn_id)
        action = Action(
            id="old-edit-reply",
            tenant_id=1,
            task_id=task.id,
            task_type=task.type,
            action_type="send_message",
            account_id=11,
            task_lifecycle_epoch=task.task_lifecycle_epoch,
        )
        session.add(action)
        session.flush()
        claim.action_id = action.id
        claim.state = "bound"

        _ingest(
            session,
            state,
            remote_id=103,
            content="修改后的问题",
            event_type="message_edit",
        )
        assert consume_group_ai_update_deliveries(session, task, group) == 0

        event = session.scalar(select(ConversationEvent))
        turns = list(session.scalars(select(ContextTurn).order_by(ContextTurn.created_at)))
        assert claim.state == "bound"
        assert claim.settlement_reason == ""
        assert turn.version == 1
        assert event.event_revision == 1 and event.content == "原问题"
        assert len(turns) == 1 and turns[0].id == turn.id
        assert len(list(session.scalars(select(ConversationTurnClaim)))) == 1
        assert task.stats.get("group_update_stream_reconcile_required") != "message_edit"
        assert consume_group_ai_update_deliveries(session, task, group) == 0


def test_stream_delete_removes_context_and_stales_open_reply() -> None:
    with _session() as session:
        task, group, state = _seed(session)
        _ingest(session, state, remote_id=104, content="稍后删除")
        consume_group_ai_update_deliveries(session, task, group)
        materialize_due_turns(
            session,
            task,
            group,
            now_value=datetime(2026, 9, 5, 10, 0, 6),
        )
        claim = session.scalar(select(ConversationTurnClaim))

        _ingest(
            session,
            state,
            remote_id=104,
            content="",
            event_type="message_delete",
        )
        consume_group_ai_update_deliveries(session, task, group)

        event = session.scalar(select(ConversationEvent))
        context = session.scalar(select(GroupContextMessage))
        assert event.is_current is False and event.deleted_at is not None
        assert context.content == "" and context.message_type == "deleted"
        assert claim.state == "stale"
        assert claim.settlement_reason == "reply_parent_deleted"


def test_listener_authorization_rebind_skips_old_pending_delivery() -> None:
    with _session() as session:
        task, group, old_state = _seed(session)
        _ingest(session, old_state, remote_id=105, content="旧监听器消息")
        account = session.get(TgAccount, 11)
        old_authorization = session.get(TgAccountAuthorization, 21)
        old_authorization.is_current = False
        old_authorization.is_slot_current = False
        replacement = TgAccountAuthorization(
            id=22,
            tenant_id=1,
            account_id=11,
            slot_generation=2,
            is_current=True,
            is_slot_current=True,
            status="active",
            session_ciphertext="replacement-session",
        )
        session.add(replacement)
        session.flush()
        account.current_authorization_id = replacement.id

        assert ensure_group_ai_update_subscription(
            session,
            task,
            group,
            listener_account_id=account.id,
        )

        delivery = session.scalar(select(TelegramAuthorizationUpdateDelivery))
        subscription = session.scalar(select(TelegramAuthorizationUpdateSubscription))
        replacement_state = session.get(
            TelegramAuthorizationUpdateState,
            subscription.authorization_update_state_id,
        )
        assert delivery.delivery_state == "skipped"
        assert replacement_state.authorization_id == replacement.id
        assert task.stats["group_update_stream_reconcile_required"] == "listener_route_changed"

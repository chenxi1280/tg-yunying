from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.database import Base
from app.models import (
    Action,
    ChannelMessage,
    CommentFulfillmentObligation,
    OperationTarget,
    Task,
    TaskDayLedger,
    Tenant,
    TgAccount,
    TgGroup,
    ViewFulfillmentObligation,
)
from app.services._common import _now
from app.services.task_center.channel_fulfillment import (
    view_account_ids_for_messages,
    view_daily_counts,
)
from app.services.task_center import dispatcher, service
from app.services.task_center.comment_fulfillment_takeover import (
    ensure_comment_action_contract,
)
from app.services.task_center.fulfillment_takeover import (
    FULFILLMENT_CONTRACT_VERSION,
    UNIFIED_TASK_GATE_LIMIT,
    normalize_fulfillment_pacing,
    takeover_task,
)


pytestmark = pytest.mark.no_postgres


@pytest.mark.parametrize(
    "task_type",
    [
        "group_ai_chat",
        "channel_comment",
        "channel_like",
        "channel_view",
        "search_click",
    ],
)
def test_fulfillment_hourly_limit_is_a_system_gate(task_type: str) -> None:
    pacing = normalize_fulfillment_pacing(
        task_type,
        {"max_actions_per_hour": 1},
    )

    assert pacing["max_actions_per_hour"] == UNIFIED_TASK_GATE_LIMIT
    if task_type == "search_click":
        assert pacing["max_actions_per_day"] == UNIFIED_TASK_GATE_LIMIT


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as current:
        current.add(Tenant(id=1, name="单用户"))
        current.commit()
        yield current


def test_dispatcher_takes_over_legacy_task_before_gateway(session: Session) -> None:
    session.add(TgGroup(id=32, tenant_id=1, tg_peer_id="-10032", title="接管群"))
    task = Task(
        id="dispatch-takeover",
        tenant_id=1,
        name="部署瞬间接管",
        type="group_ai_chat",
        status="running",
        type_config={"daily_message_target": 1, "target_group_id": 32},
    )
    old_action = Action(
        id="dispatch-old-action",
        tenant_id=1,
        task_id=task.id,
        task_type=task.type,
        action_type="send_message",
        status="executing",
        payload={"message": "旧计划"},
    )
    session.add_all([task, old_action])
    session.flush()

    processed = _dispatch(session, old_action)

    assert processed is True
    assert old_action.status == "skipped"
    assert old_action.result["error_code"] == (
        "legacy_action_retired_by_fulfillment_takeover"
    )
    assert task.stats["fulfillment_contract_version"] == (
        FULFILLMENT_CONTRACT_VERSION
    )


def test_dispatcher_records_structural_takeover_blocker(session: Session) -> None:
    task = Task(
        id="invalid-contract-task",
        tenant_id=1,
        name="缺目标的任务",
        type="group_ai_chat",
        status="running",
    )
    action = Action(
        id="invalid-contract-action",
        tenant_id=1,
        task_id=task.id,
        task_type=task.type,
        action_type="send_message",
        status="executing",
    )
    session.add_all([task, action])
    session.flush()

    processed = _dispatch(session, action)

    assert processed is True
    assert action.status == "failed"
    assert action.result["error_code"] == "task_fulfillment_contract_invalid"
    assert task.stats["fulfillment_takeover_status"] == "blocked"
    assert "target group not found" in task.stats["fulfillment_takeover_error"]


def test_planner_pauses_only_the_task_with_invalid_contract(
    session: Session,
) -> None:
    task = Task(
        id="invalid-planner-contract",
        tenant_id=1,
        name="Planner 缺目标",
        type="group_ai_chat",
        status="running",
    )
    session.add(task)
    session.commit()
    factory = sessionmaker(
        bind=session.get_bind(),
        autoflush=False,
        autocommit=False,
        future=True,
    )

    processed, planned, future_open, _pending = service._plan_due_task_batch(
        factory,
        task.id,
        None,
        limit=10,
        plan_limit=1,
        global_pending=0,
    )
    session.refresh(task)

    assert (processed, planned, future_open) == (1, 0, False)
    assert task.status == "paused"
    assert task.stats["fulfillment_takeover_blocker_code"] == (
        "task_contract_invalid"
    )
    assert "target group not found" in task.last_error


def test_dispatcher_records_invalid_comment_contract_without_crashing(
    session: Session,
) -> None:
    task = Task(
        id="invalid-comment-task",
        tenant_id=1,
        name="缺消息的评论任务",
        type="channel_comment",
        status="running",
        stats={"fulfillment_contract_version": FULFILLMENT_CONTRACT_VERSION},
    )
    action = Action(
        id="invalid-comment-action",
        tenant_id=1,
        task_id=task.id,
        task_type=task.type,
        action_type="post_comment",
        status="executing",
        payload={},
    )
    session.add_all([task, action])
    session.flush()

    processed = _dispatch(session, action)

    assert processed is True
    assert action.status == "failed"
    assert action.result["error_code"] == "task_fulfillment_contract_invalid"
    assert task.stats["fulfillment_takeover_status"] == "blocked"
    assert "message_missing" in task.stats["fulfillment_takeover_error"]


def test_retrying_comment_rebinds_its_existing_obligation(
    session: Session,
) -> None:
    channel = OperationTarget(
        id=41,
        tenant_id=1,
        target_type="channel",
        tg_peer_id="-10041",
        title="评论频道",
    )
    message = ChannelMessage(
        id=51,
        tenant_id=1,
        channel_target_id=channel.id,
        message_id=901,
    )
    task = Task(
        id="retry-comment-task",
        tenant_id=1,
        name="重试评论",
        type="channel_comment",
        status="running",
    )
    obligation = CommentFulfillmentObligation(
        id="retry-comment-obligation",
        tenant_id=1,
        task_id=task.id,
        channel_message_id=message.id,
        comment_plan_revision=1,
        target_ordinal=1,
        relation_kind="direct",
        action_attempt_no=1,
        status="replan_required",
    )
    action = Action(
        id="retry-comment-action",
        tenant_id=1,
        task_id=task.id,
        task_type=task.type,
        action_type="post_comment",
        status="executing",
        payload={
            "channel_message_id": message.id,
            "comment_fulfillment_obligation_id": obligation.id,
        },
    )
    session.add_all([channel, message, task, obligation, action])
    session.flush()

    ensure_comment_action_contract(session, action, now=action.created_at)

    assert obligation.current_action_id == action.id
    assert obligation.status == "pending"
    assert obligation.action_attempt_no == 2


def test_takeover_preserves_unknown_view_as_held(
    session: Session,
) -> None:
    channel = OperationTarget(
        id=42,
        tenant_id=1,
        target_type="channel",
        tg_peer_id="-10042",
        title="浏览频道",
    )
    message = ChannelMessage(
        id=52,
        tenant_id=1,
        channel_target_id=channel.id,
        message_id=902,
    )
    account = TgAccount(
        id=102,
        tenant_id=1,
        display_name="浏览账号",
        phone_masked="102",
    )
    task = Task(
        id="unknown-view-task",
        tenant_id=1,
        name="未知浏览",
        type="channel_view",
        status="running",
    )
    action = Action(
        id="unknown-view-action",
        tenant_id=1,
        task_id=task.id,
        task_type=task.type,
        action_type="view_message",
        account_id=account.id,
        status="unknown_after_send",
        payload={
            "channel_id": channel.tg_peer_id,
            "channel_message_id": message.id,
            "message_id": message.message_id,
        },
    )
    session.add_all([channel, message, account, task, action])
    session.flush()

    takeover_task(session, task, now=_now())
    session.flush()

    obligation = session.query(ViewFulfillmentObligation).one()
    ledger = session.get(TaskDayLedger, obligation.task_day_ledger_id)
    assert obligation.status == "unknown"
    assert obligation.current_action_id == action.id
    assert view_account_ids_for_messages(
        session,
        task,
        ledger,
        [message],
    ) == {message.id: {account.id}}
    daily = view_daily_counts(session, ledger)
    assert daily.total == 1
    assert daily.by_account == {account.id: 1}


def test_dispatch_finalizer_marks_view_obligation_unknown(
    session: Session,
) -> None:
    channel = OperationTarget(
        id=43,
        tenant_id=1,
        target_type="channel",
        tg_peer_id="-10043",
        title="未知结果频道",
    )
    message = ChannelMessage(
        id=53,
        tenant_id=1,
        channel_target_id=channel.id,
        message_id=903,
    )
    account = TgAccount(
        id=103,
        tenant_id=1,
        display_name="未知结果账号",
        phone_masked="103",
    )
    task = Task(
        id="unknown-finalizer-task",
        tenant_id=1,
        name="未知结果浏览",
        type="channel_view",
        status="running",
    )
    session.add_all([channel, message, account, task])
    session.flush()
    takeover_task(session, task, now=_now())
    ledger = session.query(TaskDayLedger).one()
    obligation = ViewFulfillmentObligation(
        tenant_id=1,
        task_day_ledger_id=ledger.id,
        channel_message_id=message.id,
        account_id=account.id,
        status="pending",
    )
    session.add(obligation)
    session.flush()
    action = Action(
        id="unknown-finalizer-action",
        tenant_id=1,
        task_id=task.id,
        task_type=task.type,
        action_type="view_message",
        account_id=account.id,
        status="unknown_after_send",
        payload={"view_fulfillment_obligation_id": obligation.id},
    )
    obligation.current_action_id = action.id
    session.add(action)
    session.flush()

    dispatcher._sync_channel_fulfillment_state(session, action)

    assert obligation.status == "unknown"
    assert obligation.current_action_id == action.id


def _dispatch(session: Session, action: Action) -> bool:
    return dispatcher._dispatch_action(
        session,
        action,
        generation_dependencies=dispatcher.PRODUCTION_GENERATION_DEPENDENCIES,
        comment_generation_dependencies=(
            dispatcher.PRODUCTION_COMMENT_GENERATION_DEPENDENCIES
        ),
    )

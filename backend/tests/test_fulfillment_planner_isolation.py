from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.database import Base
from app.models import (
    Action,
    ChannelMessage,
    OperationTarget,
    ReactionFulfillmentObligation,
    Task,
    TaskDayLedger,
    Tenant,
    TgAccount,
    ViewFulfillmentObligation,
)
from app.services._common import _now
from app.services.task_center import dispatcher, service
from app.services.task_center.channel_fulfillment import ensure_view_obligation
from app.services.task_center.channel_fulfillment_queries import (
    reaction_account_ids_for_messages,
    view_account_ids_for_messages,
)
from app.services.task_center.fulfillment_takeover import takeover_task
from app.services.task_center.executors import group_ai_chat, search_click
from app.services.task_center.payloads import SendMessagePayload


pytestmark = pytest.mark.no_postgres


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as current:
        current.add(Tenant(id=1, name="单用户"))
        current.commit()
        yield current


def _view_context(session: Session) -> tuple[Task, ChannelMessage, TgAccount]:
    channel = OperationTarget(
        id=44,
        tenant_id=1,
        target_type="channel",
        tg_peer_id="-10044",
        title="浏览频道",
    )
    message = ChannelMessage(
        id=54,
        tenant_id=1,
        channel_target_id=channel.id,
        message_id=904,
    )
    account = TgAccount(
        id=104,
        tenant_id=1,
        display_name="浏览账号",
        phone_masked="104",
    )
    task = Task(
        id="view-binding-task",
        tenant_id=1,
        name="浏览绑定",
        type="channel_view",
        status="running",
    )
    session.add_all([channel, message, account, task])
    session.flush()
    takeover_task(session, task, now=_now())
    return task, message, account


@pytest.mark.parametrize("action_type", ["view_message", "like_message"])
def test_account_bound_channel_fulfillment_cannot_reassign(
    action_type: str,
) -> None:
    binding_key = (
        "view_fulfillment_obligation_id"
        if action_type == "view_message"
        else "reaction_fulfillment_obligation_id"
    )
    action = Action(
        id=f"{action_type}-bound",
        tenant_id=1,
        task_id="task-bound",
        task_type="channel_view" if action_type == "view_message" else "channel_like",
        action_type=action_type,
        account_id=1,
        status="claiming",
        payload={binding_key: "bound-obligation"},
    )

    assert dispatcher._action_can_reassign(action) is False


def test_reassigned_view_action_releases_stale_original_binding(
    session: Session,
) -> None:
    task, message, original = _view_context(session)
    replacement = TgAccount(
        id=105,
        tenant_id=1,
        display_name="替换账号",
        phone_masked="105",
    )
    session.add(replacement)
    session.flush()
    ledger = session.query(TaskDayLedger).one()
    original_obligation = _view_obligation(
        ledger,
        message=message,
        account=original,
        obligation_id="original-view-obligation",
    )
    replacement_obligation = _view_obligation(
        ledger,
        message=message,
        account=replacement,
        obligation_id="replacement-view-obligation",
        status="confirmed",
    )
    action = _bound_view_action(task, replacement, replacement_obligation)
    original_obligation.current_action_id = action.id
    replacement_obligation.current_action_id = action.id
    session.add_all([original_obligation, replacement_obligation, action])
    session.flush()

    resolved = ensure_view_obligation(session, ledger, message, original.id)

    assert resolved.id == original_obligation.id
    assert resolved.current_action_id is None
    assert resolved.status == "open"


def test_successful_same_view_binding_remains_held(session: Session) -> None:
    task, message, account = _view_context(session)
    ledger = session.query(TaskDayLedger).one()
    obligation = _view_obligation(
        ledger,
        message=message,
        account=account,
        obligation_id="held-success-view-obligation",
    )
    action = _bound_view_action(task, account, obligation)
    obligation.current_action_id = action.id
    session.add_all([obligation, action])
    session.flush()

    assert view_account_ids_for_messages(
        session,
        task,
        ledger,
        [message],
    ) == {message.id: {account.id}}


def test_successful_same_reaction_binding_remains_held(session: Session) -> None:
    task, message, account = _view_context(session)
    task.type = "channel_like"
    obligation = ReactionFulfillmentObligation(
        id="held-success-reaction-obligation",
        tenant_id=1,
        task_id=task.id,
        channel_message_id=message.id,
        account_id=account.id,
        reaction_contract_version=task.config_revision,
        status="pending",
    )
    action = Action(
        id="action-held-success-reaction",
        tenant_id=1,
        task_id=task.id,
        task_type=task.type,
        action_type="like_message",
        account_id=account.id,
        status="success",
        payload={"reaction_fulfillment_obligation_id": obligation.id},
    )
    obligation.current_action_id = action.id
    session.add_all([obligation, action])
    session.flush()

    assert reaction_account_ids_for_messages(
        session,
        task,
        [message],
    ) == {message.id: {account.id}}


def _view_obligation(
    ledger: TaskDayLedger,
    *,
    message: ChannelMessage,
    account: TgAccount,
    obligation_id: str,
    status: str = "pending",
) -> ViewFulfillmentObligation:
    return ViewFulfillmentObligation(
        id=obligation_id,
        tenant_id=1,
        task_day_ledger_id=ledger.id,
        channel_message_id=message.id,
        account_id=account.id,
        status=status,
    )


def _bound_view_action(
    task: Task,
    account: TgAccount,
    obligation: ViewFulfillmentObligation,
) -> Action:
    return Action(
        id=f"action-{obligation.id}",
        tenant_id=1,
        task_id=task.id,
        task_type=task.type,
        action_type="view_message",
        account_id=account.id,
        status="success",
        payload={"view_fulfillment_obligation_id": obligation.id},
    )


def test_planner_records_one_task_error_and_continues_other_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_factory = _session_factory()
    monkeypatch.setattr(
        service,
        "_normal_planner_task_ids",
        lambda *_args, **_kwargs: ["task-planner-broken", "task-planner-ready"],
    )
    monkeypatch.setattr(service, "planner_global_pending", lambda *_args: 0)

    def fake_plan(_factory, task_id, *_args, **kwargs):
        if task_id == "task-planner-broken":
            raise ValueError("fulfillment_obligation_already_bound")
        return 2, False, int(kwargs.get("global_pending") or 0)

    monkeypatch.setattr(service, "_plan_due_task", fake_plan)
    _add_planner_tasks(session_factory)

    processed, _ = service._drain_task_planner(
        session_factory,
        limit=5,
        process_type=None,
    )

    assert processed == 2
    with session_factory() as current:
        broken = current.get(Task, "task-planner-broken")
        error = broken.stats["planner_runtime_error"]
        assert error["error_type"] == "ValueError"
        assert error["message"] == "fulfillment_obligation_already_bound"


def test_content_mix_replan_reads_metadata_from_failed_empty_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    planned_at = _now()
    fresh = group_ai_chat.SlotSnapshot(
        account_id=1,
        planned_at=planned_at,
        payload=SendMessagePayload(
            group_id=1,
            ai_generation_status="pending",
        ),
    )
    monkeypatch.setattr(
        group_ai_chat,
        "_build_slot_snapshot",
        lambda *_args, **_kwargs: fresh,
    )
    blueprint = SimpleNamespace(
        generation=SimpleNamespace(times=[planned_at], quality_items=[{}]),
    )
    cycle_slot = SimpleNamespace(
        id="slot-1",
        cycle_id="cycle-1",
        relation_kind="direct",
        primary_quantity_slot_id="quantity-1",
        slot_attempt=1,
    )
    previous = Action(
        id="failed-reply-action",
        tenant_id=1,
        task_id="task-ai",
        task_type="group_ai_chat",
        action_type="send_message",
        status="failed",
        payload={
            "chat_id": "https://t.me/example",
            "message_text": "",
            "ai_generation_status": "reply_target_stale",
            "cycle_id": "legacy-cycle",
            "slot_id": "legacy-slot",
            "content_mix_contract_version": 1,
            "rule_set_id": 7,
        },
    )

    snapshot = group_ai_chat._replan_slot_snapshot(
        blueprint,
        SimpleNamespace(id=1),
        0,
        cycle_slot,
        previous,
    )

    assert snapshot.payload.ai_generation_status == "pending"
    assert snapshot.payload.content_mix_cycle_slot_id == "slot-1"
    assert snapshot.payload.cycle_id == "legacy-cycle"
    assert snapshot.payload.rule_set_id == 7


def test_search_click_finalize_restarts_before_setting_serializable() -> None:
    events: list[str] = []
    session = SimpleNamespace(
        bind=SimpleNamespace(dialect=SimpleNamespace(name="postgresql")),
        rollback=lambda: events.append("rollback"),
        execute=lambda statement: events.append(str(statement)),
    )

    search_click._restart_serializable_finalize_transaction(session)

    assert events == [
        "rollback",
        "SET TRANSACTION ISOLATION LEVEL SERIALIZABLE",
    ]


def _session_factory():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True)


def _add_planner_tasks(session_factory) -> None:
    with session_factory() as current:
        current.add(Tenant(id=1, name="默认运营空间"))
        for task_id in ("task-planner-broken", "task-planner-ready"):
            current.add(
                Task(
                    id=task_id,
                    tenant_id=1,
                    name=task_id,
                    type="channel_view",
                    status="running",
                    next_run_at=_now(),
                )
            )
        current.commit()

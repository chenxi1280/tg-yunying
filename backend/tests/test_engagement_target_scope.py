from __future__ import annotations

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
import pytest

from app.database import Base
from app.models import (
    Action,
    Campaign,
    GroupContextMessage,
    MessageTask,
    OperationTarget,
    Task,
    TaskTargetScopeClaim,
    Tenant,
    TgGroup,
)
from app.services import group_listeners, messages
from app.services.task_center import dispatcher
from app.services.task_center.engagement_target_scope import (
    TaskTargetScopeConflict,
    ensure_task_target_scope_claims,
    has_current_task_target_scope_claim,
    release_task_target_scope_claims,
)
from app.services.task_center import fulfillment_activation


pytestmark = pytest.mark.no_postgres


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as current:
        current.add(Tenant(id=1, name="tenant"))
        current.add(
            TgGroup(
                id=10,
                tenant_id=1,
                tg_peer_id="-100100",
                title="group",
            )
        )
        current.add(
            OperationTarget(
                id=20,
                tenant_id=1,
                target_type="channel",
                tg_peer_id="-100200",
                title="channel",
            )
        )
        current.commit()
        yield current


def _task(
    task_id: str,
    task_type: str,
    target_id: int,
    *,
    status: str = "running",
    epoch: int = 2,
) -> Task:
    target_field = (
        "target_group_id" if task_type == "group_ai_chat" else "target_channel_id"
    )
    return Task(
        id=task_id,
        tenant_id=1,
        name=task_id,
        type=task_type,
        status=status,
        task_lifecycle_epoch=epoch,
        fulfillment_contract_version="fact_first_v3",
        type_config={
            "engagement_contract_version": "unified_engagement_v1",
            target_field: target_id,
        },
    )


def test_same_adapter_same_peer_has_one_active_writer(session: Session) -> None:
    first = _task("group-first", "group_ai_chat", 10)
    second = _task("group-second", "group_ai_chat", 10)
    session.add_all([first, second])
    session.flush()

    first_claim = ensure_task_target_scope_claims(session, first)[0]
    with pytest.raises(TaskTargetScopeConflict, match="group-first"):
        ensure_task_target_scope_claims(session, second)

    active = list(session.scalars(select(TaskTargetScopeClaim).where(
        TaskTargetScopeClaim.state == "active"
    )))
    assert active == [first_claim]


def test_different_adapters_can_share_canonical_peer(session: Session) -> None:
    view = _task("view", "channel_view", 20)
    like = _task("like", "channel_like", 20)
    comment = _task("comment", "channel_comment", 20)
    session.add_all([view, like, comment])
    session.flush()

    claims = [
        ensure_task_target_scope_claims(session, task)[0]
        for task in (view, like, comment)
    ]

    assert {claim.adapter_type for claim in claims} == {
        "channel_view",
        "channel_like",
        "channel_comment",
    }
    assert {claim.canonical_peer_id for claim in claims} == {"-100200"}


def test_stale_holder_is_released_before_new_writer_acquires(session: Session) -> None:
    first = _task("old-writer", "group_ai_chat", 10)
    second = _task("new-writer", "group_ai_chat", 10)
    session.add_all([first, second])
    session.flush()
    old_claim = ensure_task_target_scope_claims(session, first)[0]
    first.status = "completed"

    new_claim = ensure_task_target_scope_claims(session, second)[0]

    assert old_claim.state == "released"
    assert old_claim.release_reason == "stale_holder"
    assert new_claim.state == "active"
    assert new_claim.task_id == second.id


def test_release_and_new_epoch_reacquire_are_explicit(session: Session) -> None:
    task = _task("lifecycle", "group_ai_chat", 10)
    session.add(task)
    session.flush()
    first = ensure_task_target_scope_claims(session, task)[0]

    assert release_task_target_scope_claims(session, task, reason="task_paused") == 1
    task.task_lifecycle_epoch += 1
    second = ensure_task_target_scope_claims(session, task)[0]

    assert first.state == "released"
    assert second.id != first.id
    assert second.task_lifecycle_epoch == task.task_lifecycle_epoch
    assert has_current_task_target_scope_claim(session, task)


def test_gateway_route_fails_closed_without_current_scope_claim(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _task("gateway-owner", "channel_view", 20)
    action = Action(
        id="gateway-action",
        tenant_id=1,
        task_id=task.id,
        task_type=task.type,
        action_type="view_message",
        status="executing",
        task_lifecycle_epoch=task.task_lifecycle_epoch,
    )
    session.add_all([task, action])
    session.flush()
    monkeypatch.setattr(fulfillment_activation, "gateway_task_allowed", lambda *_: True)

    assert dispatcher._fulfillment_route_allows_gateway(session, action) is False
    ensure_task_target_scope_claims(session, task)
    assert dispatcher._fulfillment_route_allows_gateway(session, action) is True


def test_unified_owner_fences_legacy_listener_campaign_creation(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _task("unified-response-owner", "group_ai_chat", 10)
    context = GroupContextMessage(
        tenant_id=1,
        group_id=10,
        listener_account_id=999,
        sender_name="真人",
        content="现在有人吗",
        remote_message_id="88",
    )
    session.add_all([task, context])
    session.flush()
    ensure_task_target_scope_claims(session, task)
    monkeypatch.setattr(
        group_listeners,
        "create_campaign",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("legacy campaign must not be created")
        ),
    )

    assert group_listeners.trigger_listener_auto_reply(session, session.get(TgGroup, 10)) == 0
    assert context.used_for_ai is False


def test_unified_owner_fences_already_queued_listener_message_task(
    session: Session,
) -> None:
    owner = _task("queued-response-owner", "group_ai_chat", 10)
    campaign = Campaign(
        id=30,
        tenant_id=1,
        group_id=10,
        title="legacy listener",
        campaign_type="监听上下文续聊",
        topic="topic",
    )
    message_task = MessageTask(
        id=31,
        tenant_id=1,
        campaign_id=campaign.id,
        group_id=10,
        content="旧链路回复",
        idempotency_key="legacy-listener-31",
        target_peer_id="-100100",
    )
    session.add_all([owner, campaign, message_task])
    session.flush()
    ensure_task_target_scope_claims(session, owner)

    assert messages._unified_listener_response_owner(
        session, message_task, "-100100"
    ) == owner.id

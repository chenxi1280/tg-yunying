"""Independent Clone contract reaches its real planner despite legacy backlog."""
from dataclasses import replace
import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.models import Action, Task
from app.models.group_clone import CloneDeliveryObligation, CloneSourceEvent, CloneSourceStreamState
from app.models.telegram_updates import TelegramAuthorizationUpdateSubscription
from app.services.task_center import service
from app.services.task_center.telegram_update_ingress import ingest_normalized_update
import test_group_clone_api as clone_api
from test_group_clone_lifecycle import _running_task
from test_group_clone_update_ingress import _ingress

pytestmark = pytest.mark.no_postgres
client_and_session = clone_api.client_and_session
LEGACY_BACKLOG = 100_000


def _deliver(session, task, *, duplicate=False):
    from app.models.telegram_updates import TelegramAuthorizationUpdateState
    stream = session.scalar(select(CloneSourceStreamState).where(CloneSourceStreamState.task_id == task.id))
    subscription = session.scalar(select(TelegramAuthorizationUpdateSubscription).where(
        TelegramAuthorizationUpdateSubscription.task_id == task.id))
    subscription.state = "active"
    stream.start_pts = stream.channel_pts = 100
    stream.start_message_id = 10
    state = session.get(TelegramAuthorizationUpdateState, stream.authorization_update_state_id)
    ingress = _ingress("clone-planner-entry", message_id=11, pts=101)
    ingress = replace(ingress, routing_peer_id=stream.source_peer_id)
    ingest_normalized_update(session, state.id, ingress,
        owner_id=state.owner_id, owner_fencing_epoch=state.owner_fencing_epoch)
    if duplicate:
        ingest_normalized_update(session, state.id,
            replace(ingress, update_identity_key="clone-planner-redelivery"),
            owner_id=state.owner_id, owner_fencing_epoch=state.owner_fencing_epoch)
    session.commit()


@pytest.mark.parametrize("duplicate", [False, True])
def test_real_planner_consumes_and_materializes_with_legacy_backlog(client_and_session, duplicate):
    client, session = client_and_session
    task = _running_task(client, session)
    _deliver(session, task, duplicate=duplicate)
    factory = sessionmaker(bind=session.get_bind(), autoflush=False)
    result = service._plan_due_task_batch(factory, task.id, None,
        limit=1, plan_limit=1, global_pending=LEGACY_BACKLOG)
    session.expire_all()
    assert not session.get(Task, task.id).stats.get("planner_backlog_blocked")
    assert session.scalar(select(CloneSourceEvent).where(CloneSourceEvent.task_id == task.id))
    obligation = session.scalar(select(CloneDeliveryObligation).where(CloneDeliveryObligation.task_id == task.id))
    action = session.scalar(select(Action).where(Action.task_id == task.id))
    assert obligation is not None and action is not None
    assert action.obligation_id == obligation.id and action.action_type == "group_clone_send"
    assert result[1] == 1
    assert len(session.scalars(select(CloneSourceEvent).where(CloneSourceEvent.task_id == task.id)).all()) == 1


@pytest.mark.parametrize("task_type,contract,stats", [
    ("group_clone", "legacy", {}),
    ("group_relay", "v2_group_clone", {}),
    ("group_clone", "legacy", {"fulfillment_contract_version": "v2_group_clone"}),
])
def test_clone_exemption_requires_persisted_type_and_contract(
    client_and_session, task_type, *, contract, stats,
):
    _, session = client_and_session
    task = Task(id="backlog-contract", tenant_id=1, name="legacy", type=task_type,
        status="running", fulfillment_contract_version=contract, stats=stats)
    session.add(task)
    session.commit()
    session.info[service.PLANNER_GLOBAL_PENDING_SESSION_KEY] = LEGACY_BACKLOG
    assert service._planning_backlog_blocked(session, task)
    assert task.stats["planner_backlog_global_pending"] == LEGACY_BACKLOG

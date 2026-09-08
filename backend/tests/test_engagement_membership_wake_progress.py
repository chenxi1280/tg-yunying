from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.models import StageWakeOutbox, TaskPlannerWakeState
from app.services._common import _now
from app.services.account_group_revisions import MEMBERSHIP_WAKE_STAGE
from app.services.task_center import engagement_membership_wake as wakes
from app.services.task_center import engagement_membership_wake_delivery as delivery
from tests.test_engagement_membership_foundation import _initialize, _seed, _session, _task

pytestmark = pytest.mark.no_postgres


def test_invalid_prefix_larger_than_batch_does_not_starve_valid_wake():
    with _session() as session:
        _seed(session)
        _initialize(session)
        task = _task(session)
        for index in range(3):
            session.add(StageWakeOutbox(tenant_id=1, aggregate_type="account_group_membership",
                aggregate_id=f"missing-{index}", aggregate_revision=1, stage=MEMBERSHIP_WAKE_STAGE,
                available_at=_now() - timedelta(days=1), state="pending"))
        session.commit()
        factory = sessionmaker(bind=session.get_bind())
        assert wakes.drain_membership_wake_transactions(factory, limit=2) == 0
        for _ in range(3):
            wakes.drain_membership_wake_transactions(factory, limit=2)
        session.expire_all()
        assert session.scalar(select(func.count(StageWakeOutbox.id)).where(StageWakeOutbox.state == "invalid")) == 3
        state = session.scalar(select(TaskPlannerWakeState).where(TaskPlannerWakeState.task_id == task.id))
        assert state is not None and state.wake_revision == 2
        assert wakes.drain_membership_wake_transactions(factory, limit=2) == 0
        session.refresh(state)
        assert state.wake_revision == 2


@pytest.mark.parametrize("change", ["paused", "epoch"])
def test_fanout_survives_restart_and_checks_current_task_owner(change):
    with _session() as session:
        _seed(session)
        revision = _initialize(session)[0]
        task = _task(session)
        parent = session.scalar(select(StageWakeOutbox).where(StageWakeOutbox.aggregate_id == revision.membership.id))
        delivery.consume_membership_wake(session, parent.id, _now())
        session.commit()
        children_before = session.scalar(select(func.count(StageWakeOutbox.id)).where(
            StageWakeOutbox.stage == delivery.TASK_MEMBERSHIP_WAKE_STAGE))
        assert children_before == 1 and parent.state == "expanded"
        if change == "paused":
            task.status = "paused"
        else:
            task.task_lifecycle_epoch += 1
        session.commit()
        wakes.drain_membership_wake_transactions(sessionmaker(bind=session.get_bind()))
        session.expire_all()
        assert session.scalar(select(TaskPlannerWakeState.id).where(TaskPlannerWakeState.task_id == task.id)) is None
        child = session.scalar(select(StageWakeOutbox).where(
            StageWakeOutbox.stage == delivery.TASK_MEMBERSHIP_WAKE_STAGE))
        assert child.state == "superseded" and child.attempt_count == 1
        assert parent.state == "delivered"

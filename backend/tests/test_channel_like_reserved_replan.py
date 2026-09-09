from datetime import timedelta

import pytest

from app.models import AccountPacingReservation, Action, ChannelMessage, OperationTarget, ReactionFulfillmentObligation, Task, TgAccount
from app.services._common import _now
from app.services.task_center.executors.channel_like import _create_like_actions, _like_source_slot
from app.services.task_center.executors.channel_like_types import LikePlanItem
from app.services.task_center.executors.channel_like_pacing import _reserved_owner_points
from app.services.task_center.pacing_persistence import freeze_pacing_owner
from app.services.task_center.pacing_persistence import PacingOwnerImmutableConflict
from app.services.task_center.reaction_backlog_replan import apply_reaction_backlog
from app.services.task_center.reaction_backlog_snapshot import preview_reaction_backlog
from app.services.task_center.source_owner_cursor import pacing_source_key_hash
from app.services.task_center.source_pacing import source_pacing_plan_hash, wall_datetime
from app.services.task_center.source_capacity_plans import SourceCapacityConflict
from tests import test_reaction_backlog_replan as backlog_fixtures


pytestmark = [pytest.mark.no_postgres, pytest.mark.allow_missing_rule_binding]
session = backlog_fixtures.session


def prepare_frozen_replan(session):
    now = wall_datetime(_now())
    task = session.get(Task, "task")
    task.created_at = now - timedelta(hours=1)
    task.type_config = {"message_active_days": 1}
    message = session.get(ChannelMessage, 1)
    message.created_at = task.created_at
    item = LikePlanItem(message, 1, "👍", 0, 2)
    owner = session.get(ReactionFulfillmentObligation, "obligation")
    source = _like_source_slot(task, item, owner=owner, source_hash=pacing_source_key_hash("-1001"))
    due = now + timedelta(hours=1)
    freeze_pacing_owner(owner, plan_hash=source_pacing_plan_hash(source, {}, seed_id="like:task"),
        slot_ordinal=0, plan_total=2, due_at=due, release_not_before_at=due, source_identity=source.owner_identity)
    reservation = session.get(AccountPacingReservation, "reservation")
    reservation.pacing_slot_key = "like:task:1:1"
    reservation.due_at = reservation.release_not_before_at = reservation.effective_claim_at = due
    reservation.source_deadline_at = source.deadline_at
    old = session.get(Action, "old-action")
    old.pacing_slot_key = reservation.pacing_slot_key
    session.add(TgAccount(id=2, tenant_id=1, display_name="QA", phone_masked="QA"))
    session.flush()
    session.add(ReactionFulfillmentObligation(id="future-owner", tenant_id=1, task_id="task",
        channel_message_id=1, account_id=2, reaction_contract_version=1, status="pending",
        task_lifecycle_epoch=1, pacing_period_key=source.pacing_period_key,
        pacing_source_key_hash=source.pacing_source_key_hash, pacing_slot_ordinal=1,
        pacing_plan_total=2, pacing_due_at=source.deadline_at, release_not_before_at=source.deadline_at))
    session.commit()
    apply_reaction_backlog(session, preview_reaction_backlog(session, backlog_fixtures.SCOPE), backlog_fixtures.OPERATION)
    session.commit()
    return task, item, due


def test_normal_planner_rebuilds_reserved_owner_before_future_source_cursor(session):
    task, item, due = prepare_frozen_replan(session)
    channel = session.get(OperationTarget, 1)
    assert _create_like_actions(session, task, channel=channel, config=task.type_config, actions=[item]) == 1
    session.commit()
    owner = session.get(ReactionFulfillmentObligation, "obligation")
    reservation = session.get(AccountPacingReservation, "reservation")
    assert owner.current_action_id not in {None, "old-action"}
    replacement = session.get(Action, owner.current_action_id)
    assert replacement.status == "pending"
    assert reservation.action_id == replacement.id and reservation.state == "bound"
    assert wall_datetime(owner.pacing_due_at) == due
    assert wall_datetime(replacement.scheduled_at) >= due
    assert wall_datetime(replacement.scheduled_at) < wall_datetime(reservation.source_deadline_at)
    assert session.get(Action, "old-action").status == "skipped"


@pytest.mark.parametrize("case", ["expired", "bound", "cancelled", "missed", "unknown_owner", "missing_frozen_due"])
def test_only_open_unbound_valid_frozen_reservations_are_reused(session, case):
    task, _, _ = prepare_frozen_replan(session)
    owner = session.get(ReactionFulfillmentObligation, "obligation")
    reservation = session.get(AccountPacingReservation, "reservation")
    if case in {"bound", "cancelled", "missed"}:
        reservation.state = case
    elif case == "expired":
        reservation.source_deadline_at = _now() - timedelta(seconds=1)
    elif case == "unknown_owner":
        owner.status = "unknown"
    else:
        owner.pacing_due_at = None
    session.flush()
    assert _reserved_owner_points(session, task,
        owners={"like:task:1:1": owner}, now_at=_now()) == {}


@pytest.mark.parametrize("field", ["account_id", "due_at"])
def test_frozen_reservation_identity_drift_is_exposed(session, field):
    task, _, due = prepare_frozen_replan(session)
    reservation = session.get(AccountPacingReservation, "reservation")
    setattr(reservation, field, 2 if field == "account_id" else due + timedelta(minutes=1))
    session.flush()
    with pytest.raises(PacingOwnerImmutableConflict, match="reaction_replan_reservation_identity_mismatch"):
        _reserved_owner_points(session, task,
            owners={"like:task:1:1": session.get(ReactionFulfillmentObligation, "obligation")}, now_at=_now())


def test_rebuilt_reservation_does_not_bypass_source_capacity_policy(session):
    task, item, _ = prepare_frozen_replan(session)
    task.pacing_config = {"source_capacity_v2_enabled": True}
    with pytest.raises(SourceCapacityConflict, match="source_capacity_policy_missing"):
        _create_like_actions(session, task, channel=session.get(OperationTarget, 1),
            config=task.type_config, actions=[item])
